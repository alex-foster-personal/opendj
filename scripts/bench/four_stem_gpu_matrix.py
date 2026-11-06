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

import argparse
import contextlib
import gc
import io
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
TRACKS_DIR: Path = REPO_ROOT / ".tmp/bench/4stem-matrix/tracks"
GATE_JSONS: tuple[Path, ...] = (
    REPO_ROOT / "scripts/bench/stem_content_gate.json",
    # Round 2, added to widen n on drums.
    REPO_ROOT / "scripts/bench/stem_content_gate_round2.json",
)
DEFAULT_OUT_JSON: Path = REPO_ROOT / "scripts/bench/four_stem_gpu_matrix.json"
GENRE_OUT_JSON: Path = REPO_ROOT / "scripts/bench/four_stem_genre_matrix.json"
# Lesson banked the hard way: the CPU sibling of this script wrote its JSON only
# at the end, was killed on cell 15 of 15, and left 14 finished measurements
# alive nowhere but a log. On a BILLED card that would also have been 14 cells
# paid for twice.
CELL_CACHE: Path = REPO_ROOT / ".tmp/bench/4stem-matrix/cells-gpu.jsonl"

GPU: str = "H100"

sys.path.insert(0, str(REPO_ROOT / "scripts/bench"))
from four_stem_common import (
    AUDIBILITY_DB,
    DEFAULT_MODEL,
    MODELS,
    OVERLAP,
    ROFORMER_ARMS,
    ROFORMER_STEM_ESTIMATE,
    ROFORMER_VIPERX_1296,
    SHIFTS,
    STEMS,
    assert_cube_complete,
    gate_flags,
    paired_deltas,
    paired_deltas_by_genre,
    parse_models,
    track_genres,
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


def _reexec_if_wrong_gpu() -> None:
    # Same re-exec reason as the farm and tier_throughput: gpu= is resolved when
    # modal_vocal_farm is IMPORTED, so the card has to be in the environment
    # before that import or the run is attributed to the wrong silicon.
    if os.environ.get("MDT_FARM_GPU") != GPU:
        argv = [sys.executable, "-m", "scripts.bench.four_stem_gpu_matrix", *sys.argv[1:]]
        os.execve(sys.executable, argv, dict(os.environ, MDT_FARM_GPU=GPU))


def _import_and_check_farm(active_demucs_models: tuple[str, ...]) -> Any:
    import modal  # noqa: F401  (proves the dep before any GPU spend)

    import scripts.modal_vocal_farm as farm

    if farm.GPU_KIND != GPU:
        raise SystemExit(
            f"error: asked for {GPU}, farm hydrated on {farm.GPU_KIND}. The "
            "re-exec did not take; refusing to attribute a measurement to the "
            "wrong card."
        )
    for model in active_demucs_models:
        if model not in farm.BAKED_MODELS:
            raise SystemExit(
                f"error: {model} is not baked into the farm image, so its cold "
                "weight download would be billed to this measurement."
            )
    farm._assert_contract_matches()
    return farm


def _load_gated_tracks(
    track_filter: set[str] | None,
) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    flags = gate_flags(*GATE_JSONS)
    track_dirs = sorted(d for d in TRACKS_DIR.iterdir() if d.is_dir())
    if track_filter is not None:
        track_dirs = [d for d in track_dirs if d.name in track_filter]
        unknown = track_filter - {d.name for d in track_dirs}
        if unknown:
            raise SystemExit(f"error: no such track dir under {TRACKS_DIR}: {sorted(unknown)}")
    if not track_dirs:
        raise SystemExit(f"error: no track windows under {TRACKS_DIR}")

    for track_dir in track_dirs:
        if track_dir.name not in flags:
            raise SystemExit(f"error: {track_dir.name} has no gate row; its window was never gated")
    return flags, track_dirs


def _load_track_truth(track_dir: Path) -> tuple[Any, int, dict[str, Any]]:
    track = track_dir.name
    mix_mono, rate = _mono_read(str(track_dir / "mixture.wav"))
    window_samples = len(mix_mono)
    truth: dict[str, Any] = {}
    for stem in STEMS:
        audio, stem_rate = _mono_read(str(track_dir / f"{stem}.wav"))
        if stem_rate != rate:
            raise SystemExit(f"error: {track} true {stem} is {stem_rate} Hz, mixture is {rate} Hz")
        if len(audio) != window_samples:
            raise SystemExit(
                f"error: {track} true {stem} is {len(audio)} samples, mixture is "
                f"{window_samples} -- the window is not sample-aligned"
            )
        truth[stem] = audio
    return mix_mono, window_samples, truth


def _score_model_cell(
    farm: Any,
    track_dir: Path,
    model: str,
    mixture_bytes: bytes,
    truth: dict[str, Any],
    window_samples: int,
) -> dict[str, Any]:
    track = track_dir.name
    print(f"[gpu] {track} :: {model}", flush=True)
    result = farm.separate_track.remote(
        mixture_bytes,
        f"4stem-{model}",
        "mixture.wav",
        model,
        OVERLAP,
        SHIFTS,
        "asset-store",  # the only dest that hands the stem bytes back
        str(track_dir / "mixture.wav"),
        {"tag": f"{model}-ov{OVERLAP}", "model": model, "overlap": OVERLAP, "shifts": SHIFTS, "rung": -1},
        "",
        "flac",
    )
    if "error" in result:
        raise SystemExit(f"error: {track}/{model}: {result['error']}")
    if "stems" not in result:
        raise SystemExit(f"error: {track}/{model}: farm returned no stems")
    scores: dict[str, float] = {}
    for stem in STEMS:
        estimate, _rate = _mono_read(io.BytesIO(result["stems"][stem]))
        if len(estimate) != window_samples:
            raise SystemExit(
                f"error: {track}/{model} {stem} came back {len(estimate)} "
                f"samples, window is {window_samples}"
            )
        scores[stem] = _si_sdr_db(truth[stem], estimate)
        del estimate
    cell = {
        "track": track,
        "model": model,
        "gpu": GPU,
        "si_sdr": scores,
        "separate_s": result["timings"]["separate_s"],
        "load_s": result["timings"]["load_s"],
        "container_s": result["container_s"],
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _persist(cell)  # the moment it exists, before anything else
    print(
        "      "
        + "  ".join(f"{s}={scores[s]:+.3f}" for s in STEMS)
        + f"   sep {cell['separate_s']}s container {cell['container_s']}s",
        flush=True,
    )
    del result
    return cell


def _score_roformer_cell(
    spike: Any,
    track_dir: Path,
    mixture_bytes: bytes,
    truth: dict[str, Any],
    window_samples: int,
    run_id: str,
) -> dict[str, Any]:
    track = track_dir.name
    model = ROFORMER_VIPERX_1296
    print(f"[gpu] {track} :: {model} (modal_roformer_spike)", flush=True)
    overlap_rf = 8
    segment_size = 256
    config_tag = f"bs-roformer-viperx1296-ov{overlap_rf}-seg{segment_size}"
    result = spike.separate_track.remote(
        mixture_bytes,
        f"4stem-{model}",
        track_dir.name.replace(" ", "-")[:80],
        spike.BS_ROFORMER_VIPERX_1296,
        overlap_rf,
        segment_size,
        False,
        0,
        config_tag,
        str(track_dir / "mixture.wav"),
        "flac",
        run_id,
    )
    if "error" in result:
        raise SystemExit(f"error: {track}/{model}: {result['error']}")
    scores: dict[str, float] = {}
    for stem in STEMS:
        est_key = ROFORMER_STEM_ESTIMATE[stem]
        estimate, _rate = _mono_read(io.BytesIO(result[est_key]))
        if len(estimate) != window_samples:
            raise SystemExit(
                f"error: {track}/{model} {stem} ({est_key}) came back {len(estimate)} "
                f"samples, window is {window_samples}"
            )
        scores[stem] = _si_sdr_db(truth[stem], estimate)
        del estimate
    cell = {
        "track": track,
        "model": model,
        "gpu": GPU,
        "si_sdr": scores,
        "separate_s": result["meta"]["timings"]["separate_s"],
        "load_s": result["meta"]["timings"]["load_s"],
        "container_s": result["gpu_s"],
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _persist(cell)
    print(
        "      "
        + "  ".join(f"{s}={scores[s]:+.3f}" for s in STEMS)
        + f"   sep {cell['separate_s']}s container {cell['container_s']}s",
        flush=True,
    )
    del result
    return cell


def _measure_demucs_matrix(
    farm: Any,
    demucs_models: tuple[str, ...],
    track_dirs: list[Path],
    done: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, dict[str, float]]:
    # ONE TRACK RESIDENT AT A TIME. The first version of this loop hoisted every
    # track's truth arrays and mixture bytes above the modal context, which on a
    # 5-track run was already ~0.5 GB of float64 and would scale linearly with the
    # sample. This Mac is deep in swap, so the audio for a track is loaded, scored
    # and dropped before the next track is opened.
    # Do not open a modal app just to re-read the cache: a fully resumed run
    # should cost nothing at all.
    floors: dict[str, dict[str, float]] = {}
    any_todo = any((d.name, m) not in done for d in track_dirs for m in demucs_models)
    with farm.app.run() if any_todo else contextlib.nullcontext():
        for track_dir in track_dirs:
            track = track_dir.name
            mix_mono, window_samples, truth = _load_track_truth(track_dir)
            floors[track] = {s: _si_sdr_db(truth[s], mix_mono) for s in STEMS}
            del mix_mono

            todo = [m for m in demucs_models if (track, m) not in done]
            mixture_bytes = (track_dir / "mixture.wav").read_bytes() if todo else b""
            for model in todo:
                cell = _score_model_cell(farm, track_dir, model, mixture_bytes, truth, window_samples)
                done[(track, model)] = cell
            del truth, mixture_bytes
            gc.collect()
    return floors


def _measure_roformer_matrix(
    spike: Any,
    track_dirs: list[Path],
    done: dict[tuple[str, str], dict[str, Any]],
    run_id: str,
) -> None:
    any_todo = any((d.name, ROFORMER_VIPERX_1296) not in done for d in track_dirs)
    with spike.app.run() if any_todo else contextlib.nullcontext():
        if any_todo:
            # from_name is lazy: hydrate so the Dict exists before a remote opens it with create=False.
            spike._progress_dict(run_id, create=True).hydrate()
        for track_dir in track_dirs:
            track = track_dir.name
            if (track, ROFORMER_VIPERX_1296) in done:
                continue
            _, window_samples, truth = _load_track_truth(track_dir)
            mixture_bytes = (track_dir / "mixture.wav").read_bytes()
            cell = _score_roformer_cell(spike, track_dir, mixture_bytes, truth, window_samples, run_id)
            done[(track, ROFORMER_VIPERX_1296)] = cell
            del truth, mixture_bytes
            gc.collect()


def _assemble_cube(
    track_dirs: list[Path],
    done: dict[tuple[str, str], dict[str, Any]],
    active_models: tuple[str, ...],
) -> tuple[dict[str, dict[str, dict[str, float]]], list[dict[str, Any]], float]:
    cube: dict[str, dict[str, dict[str, float]]] = {}
    timings: list[dict[str, Any]] = []
    container_s_total = 0.0
    for track_dir in track_dirs:
        track = track_dir.name
        cube[track] = {}
        for model in active_models:
            cell = done.get((track, model))
            if cell is None:
                raise RuntimeError(f"{track}/{model} never measured, the cube has a hole")
            missing = [s for s in STEMS if s not in cell["si_sdr"]]
            if missing:
                raise RuntimeError(f"{track}/{model} has no score for {missing}")
            cube[track][model] = cell["si_sdr"]
            container_s_total += float(cell["container_s"])
            timings.append(
                {
                    "track": track,
                    "model": model,
                    "separate_s": cell["separate_s"],
                    "load_s": cell["load_s"],
                    "container_s": cell["container_s"],
                    # The window is 60s, so separate_s IS seconds per audio-minute.
                    "gpu_s_per_audio_minute": round(float(cell["separate_s"]), 2),
                }
            )
    return cube, timings, container_s_total


@dataclass(frozen=True, slots=True)
class _GpuMatrixPayloadInputs:
    farm: Any | None
    spike: Any | None
    flags: dict[str, dict[str, Any]]
    floors: dict[str, dict[str, float]]
    cube: dict[str, dict[str, dict[str, float]]]
    timings: list[dict[str, Any]]
    container_s_total: float
    active_models: tuple[str, ...]
    out_path: Path
    roformer_run_id: str | None


def _build_payload(ctx: _GpuMatrixPayloadInputs) -> dict[str, Any]:
    farm = ctx.farm
    spike = ctx.spike
    flags = ctx.flags
    floors = ctx.floors
    cube = ctx.cube
    timings = ctx.timings
    container_s_total = ctx.container_s_total
    active_models = ctx.active_models
    out_path = ctx.out_path
    roformer_run_id = ctx.roformer_run_id
    tg = track_genres(cube.keys())
    assert_cube_complete(cube, active_models)
    arm_layouts: dict[str, Any] = {}
    for m in active_models:
        if m in ROFORMER_ARMS:
            arm_layouts[m] = {
                "layout": "roformer2",
                "stem_estimates": dict(ROFORMER_STEM_ESTIMATE),
            }
        else:
            arm_layouts[m] = {"layout": "demucs4", "stem_estimates": {s: s for s in STEMS}}

    knobs: dict[str, Any] = {
        "demucs": {"overlap": OVERLAP, "shifts": SHIFTS, "sample_rate_hz": 44100},
    }
    if spike is not None and ROFORMER_VIPERX_1296 in active_models:
        knobs["roformer_mdxc"] = {
            "checkpoint": spike.BS_ROFORMER_VIPERX_1296,
            "overlap": 8,
            "segment_size": 256,
            "override_segment_size": False,
            "pitch_shift": 0,
            "force_format": "flac",
            "run_id": roformer_run_id,
        }

    usd_rate: float
    if farm is not None:
        usd_rate = float(farm._gpu_usd_per_s())
    elif spike is not None:
        usd_rate = float(spike._GPU_USD_PER_S[spike.GPU_KIND])
    else:
        raise RuntimeError("internal: neither farm nor spike for cost rate")

    runners: list[str] = []
    if farm is not None:
        runners.append("scripts/modal_vocal_farm.py::separate_track (the production entrypoint)")
    if spike is not None:
        runners.append("scripts/modal_roformer_spike.py::separate_track (BS-RoFormer viperx-1296)")

    return {
        "question": (
            "Does the per-stem model inversion in ladder_4stem_all.json (plain htdemucs "
            "wins bass by 0.775 dB, hdemucs_mmi wins vocals by 0.790) hold across tracks, "
            "and where does the never-scored htdemucs_ft land per stem?"
        ),
        "dataset": "MUSDB18-HQ test",
        "reference_kind": "TRUE MUSDB18-HQ stem",
        "window_length_s": 60.0,
        "windows_chosen_by": "scripts/bench/stem_content_gate.py, before any model score existed",
        "windows_verified_by": ".tmp/bench/4stem-matrix/verify_fetch.py",
        "knobs": knobs,
        "default_model": DEFAULT_MODEL,
        "models": list(active_models),
        "track_genres": tg,
        "arm_layouts": arm_layouts,
        "audibility_bar_db": AUDIBILITY_DB,
        "device": "cuda",
        "hardware": f"Modal {GPU}",
        "runner": " + ".join(runners),
        "n_tracks": len(cube),
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "gate": {t: flags[t] for t in cube},
        "mixture_floor_si_sdr": floors,
        "si_sdr": cube,
        "paired_deltas_vs_default": paired_deltas(cube, flags, models=active_models),
        "paired_deltas_vs_default_by_genre": paired_deltas_by_genre(cube, flags, tg, models=active_models),
        "timings": timings,
        "container_s_total": round(container_s_total, 1),
        "gpu_usd_spent": round(container_s_total * usd_rate, 4),
        "json_out": str(out_path.relative_to(REPO_ROOT)),
    }


def _print_score_tables(
    cube: dict[str, dict[str, dict[str, float]]],
    floors: dict[str, dict[str, float]],
    flags: dict[str, dict[str, Any]],
    payload: dict[str, Any],
    active_models: tuple[str, ...],
    out_json: Path,
) -> None:
    width = max(len(t) for t in cube) + 2
    for stem in STEMS:
        print(f"\n=== {stem} (SI-SDR dB vs TRUE stem, 60s window, {GPU}) ===")
        print("track".ljust(width) + "".join(m.rjust(14) for m in active_models) + "         floor  gated")
        for track in sorted(cube):
            row = track.ljust(width)
            row += "".join(f"{cube[track][m][stem]:14.3f}" for m in active_models)
            row += f"{floors[track][stem]:14.3f}"
            row += "  yes" if flags[track]["stem_passed"][stem] else "   NO"
            print(row)
    print(f"\ncontainer {payload['container_s_total']}s on {GPU} = ${payload['gpu_usd_spent']}")
    print(f"[OK] wrote {out_json}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models",
        default=None,
        help=f"comma-separated models (default: {','.join(MODELS)})",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="output JSON path (default: four_stem_gpu_matrix.json, or four_stem_genre_matrix.json "
        "when RoFormer is in --models)",
    )
    parser.add_argument("--track", action="append", default=None, help="limit to these track dir names")
    args = parser.parse_args()

    active_models = parse_models(args.models)
    demucs_models = tuple(m for m in active_models if m not in ROFORMER_ARMS)
    wants_roformer = any(m in ROFORMER_ARMS for m in active_models)

    if demucs_models:
        _reexec_if_wrong_gpu()
        farm = _import_and_check_farm(demucs_models)
    else:
        farm = None

    track_filter = set(args.track) if args.track else None
    flags, track_dirs = _load_gated_tracks(track_filter)
    done = _load_cells()
    if done:
        print(f"[resume] {len(done)} cell(s) already measured on {GPU}", flush=True)

    json_out = args.json_out
    if json_out is None:
        json_out = GENRE_OUT_JSON if wants_roformer else DEFAULT_OUT_JSON

    roformer_run_id: str | None = None
    spike: Any | None = None
    if wants_roformer:
        roformer_run_id = f"4stem-genre-{uuid.uuid4().hex[:12]}"
        import scripts.modal_roformer_spike as spike_mod

        spike = spike_mod
        if spike.GPU_KIND != GPU:
            raise SystemExit(
                f"error: RoFormer spike expects {GPU}, got {spike.GPU_KIND} "
                "(scripts.modal_roformer_spike.GPU_KIND)"
            )

    floors: dict[str, dict[str, float]] = {}
    if farm is not None:
        floors = _measure_demucs_matrix(farm, demucs_models, track_dirs, done)
    elif demucs_models:
        raise SystemExit("internal: demucs models requested but farm not loaded")

    if spike is not None:
        assert roformer_run_id is not None
        _measure_roformer_matrix(spike, track_dirs, done, roformer_run_id)
        # Floors from demucs path already cover every track; if RoFormer-only, compute floors.
        if farm is None:
            for track_dir in track_dirs:
                track = track_dir.name
                mix_mono, _ws, truth = _load_track_truth(track_dir)
                floors[track] = {s: _si_sdr_db(truth[s], mix_mono) for s in STEMS}
                del mix_mono, truth
                gc.collect()

    cube, timings, container_s_total = _assemble_cube(track_dirs, done, active_models)
    payload = _build_payload(
        _GpuMatrixPayloadInputs(
            farm=farm,
            spike=spike,
            flags=flags,
            floors=floors,
            cube=cube,
            timings=timings,
            container_s_total=container_s_total,
            active_models=active_models,
            out_path=json_out,
            roformer_run_id=roformer_run_id,
        )
    )
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _print_score_tables(cube, floors, flags, payload, active_models, json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
