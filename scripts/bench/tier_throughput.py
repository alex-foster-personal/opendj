"""Measure what each S/M/L tier actually costs on a given Modal card.

THE ONLY SANCTIONED WRITER of scripts/bench/tier_throughput.json, which is the
only thing apps/stems/tiers.py will estimate from. Nothing here is typed by
hand: a number that no script emits is a transcription waiting to happen, and
this project has already paid for that three times in one session.

METHOD. Separation cost is close to linear in track length, so each tier is run
over several tracks of DIFFERENT durations and a straight line is fitted:

    cost = fixed + audio_minutes * slope

One duration cannot separate ``fixed`` from ``slope`` -- any single point admits
infinitely many lines -- so the fit REFUSES fewer than three distinct durations.
Two clocks are fitted separately: client wall (what a caller waits, cold start
included) and container seconds (what Modal bills, cold start excluded).

  uv run --with modal python -m scripts.bench.tier_throughput --gpu H100
  uv run --with modal python -m scripts.bench.tier_throughput --gpu H100 --tier L

MINI-PRD
--------
  ✔︎ ✅ 🎯 fit each requested tier over >= 3 distinct durations, on ONE card.
    [if] fewer than 3 distinct durations are available [then ⛔️] refuse, naming
         how many were found -- never fit a line to two points and call it a
         measurement
    [if] a tier's model is not baked into the farm image [then ⛔️] refuse
         before spending GPU time, because the cold weight download would land
         in the fixed term and corrupt the fit
    [if] r-squared on the wall fit is below MIN_R_SQUARED [then] the row is
         still written but carries the note, so a bad fit is visible rather
         than laundered into a confident estimate

  ✔︎ ✅ every emitted row carries the command that produced it, the date, the
    duration span and the fit quality.
    [if] a row exists with no ``measured_by`` [then ⛔️] it did not come from
         here and must not be trusted

  → choosing which card to run on. That is apps/stems/tiers.py.
  → any claim about batch throughput. This measures ONE track at a time on
    purpose: a batch number would fold in the upload feeder, which is a
    different constraint with a different fix.

-Claude
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ----- CFG -------------------------------------------------------------------
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
OUT_JSON: Path = REPO_ROOT / "scripts/bench/tier_throughput.json"
# Three durations spanning a realistic library: a short edit, a club track, an
# extended mix. Picked by CLOSEST MATCH from the real gap, never synthesised --
# a resampled or padded file would measure the padding.
TARGET_DURATIONS_S: tuple[float, ...] = (150.0, 330.0, 600.0)
DURATION_TOLERANCE: float = 0.45  # accept a track within +/-45% of target
MIN_DISTINCT_DURATIONS: int = 3
MIN_R_SQUARED: float = 0.90
REPEATS: int = 1  # per (tier, duration); --repeats raises it


# ----- fit -------------------------------------------------------------------
def _least_squares(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Return (intercept, slope, r_squared) for y = intercept + slope * x."""
    n = len(xs)
    if n < 2:
        raise ValueError("need at least 2 points")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("all x identical -- cannot separate fixed from slope")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=False))
    r2 = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
    return intercept, slope, r2


# ----- track selection -------------------------------------------------------
def _pick_tracks(data_dir: Path, wanted: tuple[float, ...]) -> list[Any]:
    """Closest real track to each target duration, from the farm's own gap.

    Uses the gap rather than an arbitrary folder so the benchmark measures the
    same population production will run on.
    """
    from scripts.modal_vocal_farm import compute_gap

    gap = [t for t in compute_gap(data_dir) if t.duration_ms > 0]
    if not gap:
        raise SystemExit(
            "error: no gap tracks with a known duration. The fit needs real "
            "durations; refusing to guess from file size."
        )
    picked: list[Any] = []
    used: set[str] = set()
    for target in wanted:
        candidates = [
            t for t in gap
            if t.stable_id not in used
            and abs(t.duration_ms / 1000.0 - target) <= target * DURATION_TOLERANCE
        ]
        if not candidates:
            continue
        best = min(candidates, key=lambda t: abs(t.duration_ms / 1000.0 - target))
        used.add(best.stable_id)
        picked.append(best)
    return picked


# ----- run -------------------------------------------------------------------
def _warmup(farm: Any, tier: Any, track: Any) -> None:
    """One discarded separation, to take the cold start out of the fit.

    EARNED THE HARD WAY. The first run of this benchmark measured tier S first,
    so S's first track paid container cold start and weight load while the later
    tiers ran warm. The fit came back with a NEGATIVE slope (longer tracks
    cheaper) at r^2 0.27, which is physically impossible and was caught only
    because MIN_R_SQUARED gates the result. Without the gate it would have
    shipped as an estimate.
    """
    print(f"  [warmup {tier.key}] discarded, removes cold start from the fit",
          flush=True)
    farm.separate_track.remote(
        track.audio_path.read_bytes(), track.stable_id, track.audio_path.name,
        tier.model, tier.overlap, tier.shifts, "none", str(track.audio_path),
        {"tag": tier.preset_tag, "model": tier.model, "overlap": tier.overlap,
         "shifts": tier.shifts, "rung": -1},
        "",
    )


def _measure_tier(
    farm: Any, tier: Any, tracks: list[Any], repeats: int = REPEATS
) -> list[dict[str, Any]]:
    _warmup(farm, tier, tracks[0])
    rows: list[dict[str, Any]] = []
    for track in tracks:
        audio = track.audio_path.read_bytes()
        dur_s = track.duration_ms / 1000.0
        for rep in range(repeats):
            t0 = time.perf_counter()
            result = farm.separate_track.remote(
                audio,
                track.stable_id,
                track.audio_path.name,
                tier.model,
                tier.overlap,
                tier.shifts,
                "none",  # stems stay in the container: this measures separation
                str(track.audio_path),
                {"tag": tier.preset_tag, "model": tier.model,
                 "overlap": tier.overlap, "shifts": tier.shifts, "rung": -1},
                "",
            )
            wall_s = time.perf_counter() - t0
            if "error" in result:
                raise SystemExit(
                    f"error: tier {tier.key} failed on {track.stable_id}: "
                    f"{result['error']}"
                )
            rows.append({
                "tier": tier.key,
                "stable_id": track.stable_id,
                "rep": rep,
                "duration_s": round(dur_s, 1),
                "audio_minutes": round(dur_s / 60.0, 4),
                "wall_s": round(wall_s, 2),
                "container_s": result["container_s"],
                "timings": result.get("timings", {}),
            })
            print(
                f"  {tier.key} {track.stable_id[:12]} "
                f"{dur_s / 60.0:5.2f}min -> wall {wall_s:6.1f}s "
                f"container {result['container_s']:6.1f}s",
                flush=True,
            )
    return rows


def _fit(tier_key: str, gpu: str, rows: list[dict[str, Any]], cmd: str) -> dict:
    """Fit over the MEDIAN of each duration's repeats, not every raw point.

    WHY MEDIAN. Wall clock carries client-side network noise that container time
    does not: one run showed wall 10.0s against a container time of 2.9s, a
    local hiccup with no GPU meaning, and fitting it raw dropped r^2 to 0.61.
    The median of repeats is the robust summary; a mean would have absorbed the
    outlier and quietly inflated the estimate instead of being caught.

    With --repeats 1 this is identical to fitting the raw points.
    """
    distinct = {r["duration_s"] for r in rows}
    if len(distinct) < MIN_DISTINCT_DURATIONS:
        raise SystemExit(
            f"error: tier {tier_key} got {len(distinct)} distinct duration(s) "
            f"({sorted(distinct)}), need {MIN_DISTINCT_DURATIONS}. A line "
            "through fewer points cannot separate fixed cost from slope."
        )
    by_duration: dict[float, list[dict[str, Any]]] = {}
    for row in rows:
        by_duration.setdefault(row["duration_s"], []).append(row)
    points = sorted(
        (
            reps[0]["audio_minutes"],
            statistics.median(r["wall_s"] for r in reps),
            statistics.median(r["container_s"] for r in reps),
        )
        for reps in by_duration.values()
    )
    xs = [p[0] for p in points]
    wall_fixed, wall_slope, wall_r2 = _least_squares(xs, [p[1] for p in points])
    gpu_fixed, gpu_slope, _ = _least_squares(xs, [p[2] for p in points])
    note = ""
    if wall_r2 < MIN_R_SQUARED:
        note = (
            f"POOR FIT: r^2={wall_r2:.3f} below {MIN_R_SQUARED}. Cost is not "
            "clean-linear over this span; treat estimates as indicative and "
            "re-measure with more points before quoting them."
        )
    return {
        "tier_key": tier_key,
        "gpu": gpu,
        "wall_fixed_s": round(wall_fixed, 2),
        "wall_s_per_audio_minute": round(wall_slope, 2),
        "gpu_fixed_s": round(gpu_fixed, 2),
        "gpu_s_per_audio_minute": round(gpu_slope, 2),
        "n_tracks": len(rows),
        "duration_span_s": sorted(distinct),
        "r_squared": round(wall_r2, 4),
        "measured_by": cmd,
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": note,
    }


def merge_blob(
    prior: dict[str, Any],
    measurements: list[dict[str, Any]],
    raw: list[dict[str, Any]],
    gpu: str,
) -> dict[str, Any]:
    """MERGE, never replace.

    ``--tier L`` used to overwrite the whole file and silently delete the S and
    M rows measured minutes earlier, including their raw points -- so a
    targeted re-measure destroyed the comparison it was meant to refine. Rows
    are keyed by ``(tier_key, gpu)``; a fresh measurement of a pair replaces
    that pair and NOTHING else, and only that pair's raw points are dropped.

    A function rather than an inline block in ``main`` so the invariant can be
    exercised without a Modal account: everything above this line needs a GPU.
    """
    merged = {(m["tier_key"], m["gpu"]): m for m in prior.get("measurements", [])}
    replaced = {(m["tier_key"], m["gpu"]) for m in measurements}
    merged.update({(m["tier_key"], m["gpu"]): m for m in measurements})
    kept_raw = [r for r in prior.get("raw", []) if (r["tier"], gpu) not in replaced]
    return {
        "measurements": [merged[k] for k in sorted(merged)],
        "raw": kept_raw + raw,
    }


def _refit(gpu: str) -> int:
    """Re-derive the fits from raw rows on disk. No GPU, same sample."""
    blob = json.loads(OUT_JSON.read_text())
    raw = blob["raw"]
    prior = {m["tier_key"]: m for m in blob["measurements"]}
    out = []
    for tier_key in sorted({r["tier"] for r in raw}):
        rows = [r for r in raw if r["tier"] == tier_key]
        cmd = prior.get(tier_key, {}).get("measured_by", "unknown") + " (refit)"
        out.append(_fit(tier_key, gpu, rows, cmd))
    blob["measurements"] = out
    OUT_JSON.write_text(json.dumps(blob, indent=2) + "\n")
    for m in out:
        print(
            f"  {m['tier_key']}@{m['gpu']}: wall {m['wall_fixed_s']}s + "
            f"{m['wall_s_per_audio_minute']}s/audio-min (r^2 {m['r_squared']})"
            f"{' ' + m['note'] if m['note'] else ''}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.bench.tier_throughput")
    parser.add_argument("--gpu", default=None, help="Modal card (default: tier's)")
    parser.add_argument(
        "--tier", action="append", choices=("S", "M", "L"), default=None,
        help="repeatable; default is all three",
    )
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--repeats", type=int, default=REPEATS,
        help="runs per (tier, duration). Raise when two tiers land close "
             "enough that n=1 cannot tell them apart from noise.",
    )
    parser.add_argument(
        "--refit", action="store_true",
        help="recompute the fit from the raw rows already in the JSON. "
             "Changing HOW a number is derived should not cost GPU time, "
             "and re-running would silently change the sample too.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from apps.stems import tiers as tiercfg

    gpu = args.gpu or tiercfg.DEFAULT_GPU
    # Same re-exec reason as the farm: gpu= is resolved when modal_vocal_farm is
    # imported, so it has to be in the environment BEFORE that import.
    if os.environ.get("MDT_FARM_GPU") != gpu:
        os.execve(
            sys.executable,
            [sys.executable, "-m", "scripts.bench.tier_throughput"] + (
                argv if argv is not None else sys.argv[1:]
            ),
            dict(os.environ, MDT_FARM_GPU=gpu),
        )

    if args.refit:
        return _refit(gpu)

    import modal  # noqa: F401  (import proves the dep before any GPU spend)

    import scripts.modal_vocal_farm as farm
    from apps.shared.paths import DATA_DIR

    if farm.GPU_KIND != gpu:
        raise SystemExit(
            f"error: asked for {gpu}, farm hydrated on {farm.GPU_KIND}. "
            "The re-exec did not take; refusing to attribute a measurement to "
            "the wrong card."
        )
    farm._assert_contract_matches()

    keys = args.tier or ["S", "M", "L"]
    chosen = [tiercfg.get_tier(k) for k in keys]
    for tier in chosen:
        if tier.model not in farm.BAKED_MODELS:
            raise SystemExit(
                f"error: tier {tier.key} model {tier.model!r} is not baked into "
                "the image, so its cold download would land in the fixed term."
            )

    data_dir = args.data_dir or DATA_DIR
    tracks = _pick_tracks(data_dir, TARGET_DURATIONS_S)
    print(
        f"gpu={gpu} tiers={[t.key for t in chosen]} tracks="
        f"{[round(t.duration_ms / 1000.0) for t in tracks]}s"
    )
    if len(tracks) < MIN_DISTINCT_DURATIONS:
        raise SystemExit(
            f"error: found {len(tracks)} usable tracks near "
            f"{TARGET_DURATIONS_S}, need {MIN_DISTINCT_DURATIONS}. Widen "
            "DURATION_TOLERANCE or point --data-dir at a fuller library."
        )
    if args.dry_run:
        for t in tracks:
            print(f"  would measure {t.stable_id} {t.duration_ms / 1000.0:.0f}s")
        return 0

    cmd = "uv run --with modal python -m scripts.bench.tier_throughput " + " ".join(
        argv if argv is not None else sys.argv[1:]
    )
    measurements, raw = [], []
    with farm.app.run():
        for tier in chosen:
            print(f"[tier {tier.key}] {tier.preset_tag} on {gpu}")
            rows = _measure_tier(farm, tier, tracks, args.repeats)
            raw.extend(rows)
            measurements.append(_fit(tier.key, gpu, rows, cmd))

    prior = json.loads(OUT_JSON.read_text()) if OUT_JSON.exists() else {}
    OUT_JSON.write_text(
        json.dumps(merge_blob(prior, measurements, raw, gpu), indent=2) + "\n"
    )
    print(f"\nwrote {OUT_JSON.relative_to(REPO_ROOT)}")
    for m in measurements:
        print(
            f"  {m['tier_key']}@{m['gpu']}: wall {m['wall_fixed_s']}s + "
            f"{m['wall_s_per_audio_minute']}s/audio-min "
            f"(r^2 {m['r_squared']}){' ' + m['note'] if m['note'] else ''}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
