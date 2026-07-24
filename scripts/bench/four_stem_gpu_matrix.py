"""The four-stem model bake-off ON THE PRODUCTION CARD (Modal H100).

THE QUESTION. scripts/bench/ladder_4stem_all.json scored four stems on ONE track
and the model ranking INVERTED per stem: hdemucs_mmi won vocals by 0.790 dB while
plain htdemucs won bass by 0.775 dB. The library default (hdemucs_mmi, tier M)
was chosen on VOCAL SI-SDR alone, the bass gap is roughly 4x this project's
0.2 dB audibility bar, and htdemucs_ft (tier L) was never scored per stem at all.
n=1 on a laptop CPU cannot tell an inversion from noise.

WHAT IS VARIED: the model, and only the model. overlap 0.25, shifts 0, 44.1 kHz,
one pass per (track, model) on ONE card -- the same knobs apps/stems/tiers.py
ships for LOCAL / M / L. Holding the card constant matters for the same reason
tiers.py holds it constant: a model comparison spread across hardware conflates
quality with silicon.

WHAT IS SCORED: SI-SDR against the TRUE MUSDB18-HQ stem, never against another
separator's output. Scoring against a self-generated reference produced a +7 dB
artifact earlier in this project's history.

WHY THE FARM ENTRYPOINT AND NOT A FRESH MODAL APP: separate_track is what
production actually runs, its image already bakes all three checkpoints (so no
cold weight download is charged to this measurement), and reusing it means the
thing measured is the thing shipped.

THE PER-STEM GATE IS PART OF THE RESULT, not a pre-filter. Every cell is scored;
every cell carries whether THAT stem cleared stem_content_gate.py in its window.
A stem buried in its own window scores toward the mixture floor, so its delta is
not evidence -- but it stays visible instead of being quietly dropped.

SANITY ANCHOR: the do-nothing arm (the mixture graded as if it were the stem) is
computed per (track, stem). A test that cannot separate the models from doing
nothing is broken, and this is how that shows up.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 SI-SDR for every (track, model, stem) against the TRUE stem, on one card
    [if] a requested model is not baked into the farm image [then ⛔️] refuse
         BEFORE any GPU spend
    [if] the farm hydrated on a different card [then ⛔️] refuse, rather than
         attribute the measurement to the wrong silicon
    [if] a returned stem is not the window's length [then ⛔️] RuntimeError
  ✔︎ ✅ 🎯 every cell persisted the MOMENT it exists
    [if] the run is killed mid-matrix [then] finished cells survive in the JSONL
         and a re-run resumes instead of re-billing them
  ✔︎ ✅ 🎯 paired per-track deltas against the default model, gated cells only
    [if] any (track, model) cell is missing [then ⛔️] RuntimeError, never a hole
  → the CPU-vs-GPU agreement check. That is four_stem_hw_agreement.py, which
    reads this file and the CPU one.

Run (from the repo root):
  uv run --with modal python -m scripts.bench.four_stem_gpu_matrix

-Claude
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
TRACKS_DIR: Path = REPO_ROOT / ".tmp/bench/4stem-matrix/tracks"
GATE_JSON: Path = REPO_ROOT / "scripts/bench/stem_content_gate.json"
OUT_JSON: Path = REPO_ROOT / "scripts/bench/four_stem_gpu_matrix.json"
# Lesson banked the hard way: the CPU sibling of this script wrote its JSON only
# at the end, was killed on cell 15 of 15, and left 14 finished measurements
# alive nowhere but a log. On a BILLED card that would also have been 14 cells
# paid for twice.
CELL_CACHE: Path = REPO_ROOT / ".tmp/bench/4stem-matrix/cells-gpu.jsonl"

GPU: str = "H100"

sys.path.insert(0, str(REPO_ROOT / "scripts/bench"))
from four_stem_common import (  # noqa: E402
    AUDIBILITY_DB, DEFAULT_MODEL, MODELS, OVERLAP, SHIFTS, STEMS,
    gate_flags, paired_deltas,
)


def _si_sdr_db(reference: Any, estimate: Any) -> float:
    """Byte-for-byte the scorer four_stem_model_matrix.py uses, so the CPU and
    GPU cubes are comparable and both stay comparable with ladder_4stem_all."""
    import fast_bss_eval.numpy as fast_bss_eval_numpy
    import numpy as np

    n = min(len(reference), len(estimate))
    if n == 0:
        raise RuntimeError("zero-length overlap between reference and estimate")
    score = fast_bss_eval_numpy.si_sdr(reference[:n][None, :], estimate[:n][None, :])
    return round(float(np.asarray(score).reshape(-1)[0]), 3)


def _mono_read(source: Any) -> tuple[Any, int]:
    import soundfile as sf

    audio, rate = sf.read(source, dtype="float64", always_2d=True)
    return audio.mean(axis=1), int(rate)


def _load_cells() -> dict[tuple[str, str], dict[str, Any]]:
    done: dict[tuple[str, str], dict[str, Any]] = {}
    if not CELL_CACHE.is_file():
        return done
    for line in CELL_CACHE.read_text().splitlines():
        if line.strip():
            cell = json.loads(line)
            if cell["gpu"] == GPU:
                done[(cell["track"], cell["model"])] = cell
    return done


def _persist(cell: dict[str, Any]) -> None:
    CELL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CELL_CACHE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(cell) + "\n")


def main() -> int:
    # Same re-exec reason as the farm and tier_throughput: gpu= is resolved when
    # modal_vocal_farm is IMPORTED, so the card has to be in the environment
    # before that import or the run is attributed to the wrong silicon.
    if os.environ.get("MDT_FARM_GPU") != GPU:
        os.execve(sys.executable,
                  [sys.executable, "-m", "scripts.bench.four_stem_gpu_matrix"],
                  dict(os.environ, MDT_FARM_GPU=GPU))

    import modal  # noqa: F401  (proves the dep before any GPU spend)

    import scripts.modal_vocal_farm as farm

    if farm.GPU_KIND != GPU:
        raise SystemExit(
            f"error: asked for {GPU}, farm hydrated on {farm.GPU_KIND}. The "
            "re-exec did not take; refusing to attribute a measurement to the "
            "wrong card.")
    for model in MODELS:
        if model not in farm.BAKED_MODELS:
            raise SystemExit(
                f"error: {model} is not baked into the farm image, so its cold "
                "weight download would be billed to this measurement.")
    farm._assert_contract_matches()

    flags = gate_flags(GATE_JSON)
    track_dirs = sorted(d for d in TRACKS_DIR.iterdir() if d.is_dir())
    if not track_dirs:
        raise SystemExit(f"error: no track windows under {TRACKS_DIR}")

    truth: dict[str, dict[str, Any]] = {}
    floors: dict[str, dict[str, float]] = {}
    mixture_bytes: dict[str, bytes] = {}
    window_samples: dict[str, int] = {}
    for track_dir in track_dirs:
        track = track_dir.name
        if track not in flags:
            raise SystemExit(
                f"error: {track} has no row in {GATE_JSON.name}; its window was never gated")
        mixture_bytes[track] = (track_dir / "mixture.wav").read_bytes()
        mix_mono, rate = _mono_read(str(track_dir / "mixture.wav"))
        window_samples[track] = len(mix_mono)
        truth[track] = {}
        for stem in STEMS:
            audio, stem_rate = _mono_read(str(track_dir / f"{stem}.wav"))
            if stem_rate != rate:
                raise SystemExit(
                    f"error: {track} true {stem} is {stem_rate} Hz, mixture is {rate} Hz")
            if len(audio) != len(mix_mono):
                raise SystemExit(
                    f"error: {track} true {stem} is {len(audio)} samples, mixture is "
                    f"{len(mix_mono)} -- the window is not sample-aligned")
            truth[track][stem] = audio
        floors[track] = {s: _si_sdr_db(truth[track][s], mix_mono) for s in STEMS}

    done = _load_cells()
    if done:
        print(f"[resume] {len(done)} cell(s) already measured on {GPU}", flush=True)

    cube: dict[str, dict[str, dict[str, float]]] = {}
    timings: list[dict[str, Any]] = []
    container_s_total = 0.0
    todo = [(d.name, m) for d in track_dirs for m in MODELS if (d.name, m) not in done]

    if todo:
        with farm.app.run():
            for track, model in todo:
                print(f"[gpu] {track} :: {model}", flush=True)
                result = farm.separate_track.remote(
                    mixture_bytes[track], f"4stem-{model}", "mixture.wav",
                    model, OVERLAP, SHIFTS,
                    "bifrost2",  # the only dest that hands the stem bytes back
                    str(TRACKS_DIR / track / "mixture.wav"),
                    {"tag": f"{model}-ov{OVERLAP}", "model": model,
                     "overlap": OVERLAP, "shifts": SHIFTS, "rung": -1},
                    "", "flac",
                )
                if "error" in result:
                    raise SystemExit(f"error: {track}/{model}: {result['error']}")
                scores: dict[str, float] = {}
                for stem in STEMS:
                    estimate, _rate = _mono_read(io.BytesIO(result["stems"][stem]))
                    if len(estimate) != window_samples[track]:
                        raise SystemExit(
                            f"error: {track}/{model} {stem} came back "
                            f"{len(estimate)} samples, window is "
                            f"{window_samples[track]}")
                    scores[stem] = _si_sdr_db(truth[track][stem], estimate)
                cell = {
                    "track": track, "model": model, "gpu": GPU, "si_sdr": scores,
                    "separate_s": result["timings"]["separate_s"],
                    "load_s": result["timings"]["load_s"],
                    "container_s": result["container_s"],
                    "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                _persist(cell)  # the moment it exists, before anything else
                done[(track, model)] = cell
                print("      " + "  ".join(f"{s}={scores[s]:+.3f}" for s in STEMS)
                      + f"   sep {cell['separate_s']}s container {cell['container_s']}s",
                      flush=True)

    for track_dir in track_dirs:
        track = track_dir.name
        cube[track] = {}
        for model in MODELS:
            cell = done.get((track, model))
            if cell is None:
                raise RuntimeError(f"{track}/{model} never measured, the cube has a hole")
            missing = [s for s in STEMS if s not in cell["si_sdr"]]
            if missing:
                raise RuntimeError(f"{track}/{model} has no score for {missing}")
            cube[track][model] = cell["si_sdr"]
            container_s_total += float(cell["container_s"])
            timings.append({
                "track": track, "model": model,
                "separate_s": cell["separate_s"], "load_s": cell["load_s"],
                "container_s": cell["container_s"],
                # The window is 60s, so separate_s IS seconds per audio-minute.
                "gpu_s_per_audio_minute": round(float(cell["separate_s"]), 2),
            })

    payload = {
        "question": (
            "Does the per-stem model inversion in ladder_4stem_all.json (plain htdemucs "
            "wins bass by 0.775 dB, hdemucs_mmi wins vocals by 0.790) hold across tracks, "
            "and where does the never-scored htdemucs_ft land per stem?"),
        "dataset": "MUSDB18-HQ test",
        "reference_kind": "TRUE MUSDB18-HQ stem",
        "window_length_s": 60.0,
        "windows_chosen_by":
            "scripts/bench/stem_content_gate.py, before any model score existed",
        "windows_verified_by": ".tmp/bench/4stem-matrix/verify_fetch.py",
        "knobs": {"overlap": OVERLAP, "shifts": SHIFTS, "sample_rate_hz": 44100},
        "default_model": DEFAULT_MODEL,
        "models": list(MODELS),
        "audibility_bar_db": AUDIBILITY_DB,
        "device": "cuda",
        "hardware": f"Modal {GPU}",
        "runner": "scripts/modal_vocal_farm.py::separate_track (the production entrypoint)",
        "n_tracks": len(cube),
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate": {t: flags[t] for t in cube},
        "mixture_floor_si_sdr": floors,
        "si_sdr": cube,
        "paired_deltas_vs_default": paired_deltas(cube, flags),
        "timings": timings,
        "container_s_total": round(container_s_total, 1),
        "gpu_usd_spent": round(container_s_total * farm._gpu_usd_per_s(), 4),
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    width = max(len(t) for t in cube) + 2
    for stem in STEMS:
        print(f"\n=== {stem} (SI-SDR dB vs TRUE stem, 60s window, {GPU}) ===")
        print("track".ljust(width) + "".join(m.rjust(14) for m in MODELS)
              + "         floor  gated")
        for track in sorted(cube):
            row = track.ljust(width)
            row += "".join(f"{cube[track][m][stem]:14.3f}" for m in MODELS)
            row += f"{floors[track][stem]:14.3f}"
            row += "  yes" if flags[track]["stem_passed"][stem] else "   NO"
            print(row)
    print(f"\ncontainer {payload['container_s_total']}s on {GPU} "
          f"= ${payload['gpu_usd_spent']}")
    print(f"[OK] wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
