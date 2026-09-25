# /// script
# requires-python = ">=3.11"
# dependencies = ["torch", "demucs", "numpy<2", "soundfile", "fast-bss-eval>=0.1.4"]
# ///
"""Per-stem model bake-off across SEVERAL tracks: does the bass inversion hold?

scripts/bench/ladder_4stem_all.json scored four stems on ONE track and the model
ranking INVERTED per stem: hdemucs_mmi won vocals and other, plain htdemucs won
bass and drums by 0.775 dB. The project default (hdemucs_mmi, tier M) was chosen
on VOCAL SI-SDR alone, and htdemucs_ft (tier L) was never scored per-stem at all.
n=1 cannot tell an inversion from noise, so this runs the same measurement over
every gated track and adds the missing arm.

WHAT IS VARIED: the model, and only the model. overlap 0.25, shifts 0, 44.1 kHz,
one pass per (track, model), which is exactly the knob set apps/stems/tiers.py
ships (LOCAL=htdemucs, M=hdemucs_mmi, L=htdemucs_ft all sit at overlap 0.25).

WHAT IS SCORED: SI-SDR against the TRUE MUSDB18-HQ stem, never against another
separator's output. This project has already produced a +7 dB artifact by
scoring against a self-generated reference.

THE PER-STEM GATE IS PART OF THE RESULT, not a pre-filter that silently drops
data. Every cell is scored, and every cell carries whether THAT stem passed
scripts/bench/stem_content_gate.py's bars in that window. A stem that is buried
in the window regresses toward the mixture floor and its delta means nothing, so
the verdict is taken over gated cells only while the ungated ones stay visible.

SANITY ANCHOR: the mixture floor (the do-nothing arm, the mixture graded as if it
were the stem) is computed per (track, stem). Any model that fails to clear it by
a wide margin is not separating, and a test that cannot see that is broken.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 SI-SDR for every (track, model, stem) against the TRUE stem
    [if] a true stem file is missing [then ⛔️] RuntimeError naming it
    [if] a true stem and the mixture differ in rate or length [then ⛔️] RuntimeError
    [if] a model does not emit all four sources [then ⛔️] RuntimeError naming it
  ✔︎ ✅ 🎯 paired per-track deltas against the DEFAULT model, since every model
    separated the identical window (the correct statistic for this design)
    [if] a track is missing any model's score [then ⛔️] RuntimeError, no gap
  ✔︎ ✅ 🎯 one JSON carrying the full track x model x stem cube plus the gate flags
    [if] any cell is absent [then ⛔️] RuntimeError before the JSON is written

Run:
  uv run scripts/bench/four_stem_model_matrix.py \
    --tracks-dir .tmp/bench/4stem-matrix/tracks \
    --gate-json scripts/bench/stem_content_gate.json \
    --json-out scripts/bench/four_stem_model_matrix.json --device cpu

-Claude
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fast_bss_eval.numpy as fast_bss_eval_numpy
import numpy as np
import soundfile as sf
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model

sys.path.insert(0, str(Path(__file__).resolve().parent))
from four_stem_common import (
    DEFAULT_MODEL,
    MODELS,
    OVERLAP,
    SHIFTS,
    STEMS,
    gate_flags,
    paired_deltas,
)

# ----- io ---------------------------------------------------------------------


def _load(path: Path) -> tuple[torch.Tensor, int]:
    if not path.is_file():
        raise RuntimeError(f"audio file does not exist: {path}")
    audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(audio.T), int(sample_rate)


def _mono(x: torch.Tensor) -> np.ndarray:
    return x.mean(dim=0).to(torch.float64).numpy()


def _si_sdr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    """Identical maths to four_stem_ladder._si_sdr_db, so the numbers this
    writes are directly comparable with ladder_4stem_all.json."""
    n = min(len(reference), len(estimate))
    if n == 0:
        raise RuntimeError("zero-length overlap between reference and estimate")
    score = fast_bss_eval_numpy.si_sdr(reference[:n][None, :], estimate[:n][None, :])
    return round(float(np.asarray(score).reshape(-1)[0]), 3)


def load_window(track_dir: Path) -> tuple[torch.Tensor, dict[str, torch.Tensor], int]:
    mix, rate = _load(track_dir / "mixture.wav")
    truth: dict[str, torch.Tensor] = {}
    for stem in STEMS:
        audio, stem_rate = _load(track_dir / f"{stem}.wav")
        if stem_rate != rate:
            raise RuntimeError(
                f"{track_dir.name}: true {stem} is {stem_rate} Hz, mixture is {rate} Hz "
                "(refusing to resample a reference stem)")
        if audio.shape[-1] != mix.shape[-1]:
            raise RuntimeError(
                f"{track_dir.name}: true {stem} is {audio.shape[-1]} samples, mixture is "
                f"{mix.shape[-1]} -- the window is not sample-aligned")
        truth[stem] = audio
    return mix, truth, rate


# ----- separation -------------------------------------------------------------


def separate(model_name: str, mix: torch.Tensor, device: str
             ) -> tuple[dict[str, torch.Tensor], float]:
    model = get_model(model_name)
    model.eval()
    missing = [s for s in STEMS if s not in model.sources]
    if missing:
        raise RuntimeError(f"{model_name} has no sources {missing}, cannot emit 4 stems")
    start = time.perf_counter()
    with torch.no_grad():
        out = apply_model(model, mix.unsqueeze(0), overlap=OVERLAP, shifts=SHIFTS,
                          split=True, progress=False, device=device, num_workers=0)[0]
    infer_s = time.perf_counter() - start
    if out.shape[0] != len(model.sources):
        raise RuntimeError(
            f"{model_name} returned {out.shape[0]} sources, expected {len(model.sources)}")
    return {s: out[model.sources.index(s)].cpu() for s in STEMS}, infer_s


# ----- main -------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracks-dir", required=True, type=Path)
    parser.add_argument("--gate-json", required=True, type=Path)
    parser.add_argument("--json-out", required=True, type=Path)
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
    parser.add_argument(
        "--cell-cache", type=Path, default=None,
        help="JSONL appended after EVERY (track, model) cell and re-read on "
             "startup. EARNED THE HARD WAY: the first run of this script wrote "
             "its JSON only at the end, was killed on cell 15 of 15, and left "
             "14 finished measurements alive nowhere but a log. Recovering a "
             "number from a log is the near miss this project has already "
             "banked once; a cell is persisted the moment it exists instead.")
    parser.add_argument("--track", action="append", default=None,
                        help="repeatable; default is every dir under --tracks-dir")
    args = parser.parse_args()

    flags = gate_flags(args.gate_json)
    track_dirs = sorted(d for d in args.tracks_dir.iterdir() if d.is_dir())
    if args.track:
        wanted = set(args.track)
        track_dirs = [d for d in track_dirs if d.name in wanted]
        unknown = wanted - {d.name for d in track_dirs}
        if unknown:
            raise RuntimeError(f"no such track dir: {sorted(unknown)}")
    if not track_dirs:
        raise RuntimeError(f"no track dirs under {args.tracks_dir}")

    cube: dict[str, dict[str, dict[str, float]]] = {}
    floors: dict[str, dict[str, float]] = {}
    timings: list[dict[str, Any]] = []

    # Resume: a cell already measured under the SAME device is reused, because
    # separation here is deterministic (no shifts, fixed weights) and the run
    # that produced it reproduced the committed artifact to 3 decimal places.
    done: dict[tuple[str, str], dict[str, Any]] = {}
    if args.cell_cache and args.cell_cache.is_file():
        for line in args.cell_cache.read_text().splitlines():
            if not line.strip():
                continue
            cell = json.loads(line)
            if cell["device"] == args.device:
                done[(cell["track"], cell["model"])] = cell
        print(f"[resume] {len(done)} cell(s) already measured on {args.device}", flush=True)

    for track_dir in track_dirs:
        track = track_dir.name
        if track not in flags:
            raise RuntimeError(
                f"{track} has no row in {args.gate_json}; its window was never gated")
        mix, truth, rate = load_window(track_dir)
        duration_s = mix.shape[-1] / rate
        truth_mono = {s: _mono(t) for s, t in truth.items()}
        mix_mono = _mono(mix)
        floors[track] = {s: _si_sdr_db(truth_mono[s], mix_mono) for s in STEMS}
        cube[track] = {}
        for model in MODELS:
            cached = done.get((track, model))
            if cached is not None:
                print(f"[skip] {track} :: {model} (cached)", flush=True)
                scores = cached["si_sdr"]
                infer_s = cached["infer_s"]
            else:
                print(f"[run] {track} :: {model} on {args.device}", flush=True)
                stems, infer_s = separate(model, mix, args.device)
                scores = {}
                for stem in STEMS:
                    estimate = stems[stem]
                    if estimate.shape[-1] != mix.shape[-1]:
                        raise RuntimeError(
                            f"{model} {stem} is {estimate.shape[-1]} samples, mixture is "
                            f"{mix.shape[-1]}")
                    scores[stem] = _si_sdr_db(truth_mono[stem], _mono(estimate))
            missing = [s for s in STEMS if s not in scores]
            if missing:
                raise RuntimeError(f"{track}/{model} has no score for {missing}")
            cube[track][model] = scores
            timings.append({
                "track": track, "model": model,
                "infer_s": round(infer_s, 2),
                "s_per_audio_minute": round(infer_s / (duration_s / 60.0), 2),
                "from_cache": cached is not None,
            })
            if args.cell_cache and cached is None:
                with args.cell_cache.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "track": track, "model": model, "device": args.device,
                        "infer_s": round(infer_s, 2), "si_sdr": scores,
                        "measured_at": datetime.now(UTC).isoformat(
                            timespec="seconds")}) + "\n")
            print("      " + "  ".join(f"{s}={scores[s]:+.3f}" for s in STEMS), flush=True)

    payload = {
        "question": (
            "Does the per-stem model inversion in ladder_4stem_all.json (plain htdemucs "
            "wins bass by 0.775 dB, hdemucs_mmi wins vocals by 0.790) hold across tracks, "
            "and where does the never-scored htdemucs_ft land per stem?"),
        "dataset": "MUSDB18-HQ test",
        "reference_kind": "TRUE MUSDB18-HQ stem",
        "window_length_s": 60.0,
        "windows_chosen_by": "scripts/bench/stem_content_gate.py, before any model score existed",
        "knobs": {"overlap": OVERLAP, "shifts": SHIFTS, "sample_rate_hz": 44100},
        "default_model": DEFAULT_MODEL,
        "models": list(MODELS),
        "device": args.device,
        "hardware": platform.platform(),
        "n_tracks": len(cube),
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "gate": {t: flags[t] for t in cube},
        "mixture_floor_si_sdr": floors,
        "si_sdr": cube,
        "paired_deltas_vs_default": paired_deltas(cube, flags),
        "timings": timings,
    }
    args.json_out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    width = max(len(t) for t in cube) + 2
    for stem in STEMS:
        print(f"\n=== {stem} (SI-SDR dB vs TRUE stem, 60s window) ===")
        print("track".ljust(width) + "".join(m.rjust(14) for m in MODELS)
              + "         floor  gated")
        for track in sorted(cube):
            row = track.ljust(width)
            row += "".join(f"{cube[track][m][stem]:14.3f}" for m in MODELS)
            row += f"{floors[track][stem]:14.3f}"
            row += "  yes" if flags[track]["stem_passed"][stem] else "   NO"
            print(row)
    print(f"\n[OK] wrote {args.json_out}")


if __name__ == "__main__":
    main()
