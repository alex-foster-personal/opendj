# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Derive farm KPIs from per-track cache telemetry instead of hand-entering them.

Every farm-written data/state/vocal-cache/{stable_id}.json carries
``worker.timings.{load_s, separate_s, container_s}``; the file mtime is the
publication instant. That is enough to reconstruct wall clock, cost, throughput
and a bounded concurrency figure without anyone typing a number. This module is
the single source of those values; kpi_append.py consumes it.

Usage:
    uv run scripts/bench/kpi_derive.py                    # latest contiguous run
    uv run scripts/bench/kpi_derive.py --window all       # every instrumented entry
    uv run scripts/bench/kpi_derive.py --since 2026-07-24T08:40:00Z
    uv run scripts/bench/kpi_derive.py --json             # machine-readable

CONCURRENCY, AND WHY THERE ARE TWO BASES
The farm's containers stamp their own ``wall_span`` [start, end] epoch pair
(modal_vocal_farm.separate_track). When the cache entry carries it, peak and
mean concurrency are both TRUE sweep-line measurements. When it does not - as
for every entry written before wall_span was persisted - the only clock we have
is the cache-file mtime, which is the PUBLICATION time, not the container-exit
time. Modal's starmap yields in input order, so one slow call holds back every
fast call behind it: in the 128-track backlog run a single 83.7s container
published at t=85.7s and seven 7-9s containers flushed within 0.13s after it.
Reconstructing intervals as [publish - container_s, publish] therefore smears
them rightwards and invents overlap - it scores a peak of 21 against a
configured cap of 10, which is arithmetically impossible. So on the mtime
basis PEAK IS NOT DERIVABLE and is reported as None, never as a number. Mean
survives because it is a ratio of two robust totals (GPU-seconds delivered per
second of publication wall) rather than an alignment of intervals.

Requirements (mini-PRD):
- ✔︎ ✅ 🎯 R1 parse only entries carrying container_s; entries without it predate
  the instrumentation and must be excluded, never defaulted to zero.
- ✔︎ ✅ 🎯 R2 concurrency is MEASURED, never read from a configured container
  cap, and an unmeasurable peak is None rather than a plausible-looking number.
- ✔︎ ✅ 🎯 R3 track durations come from the cache entry, else state.db, else
  ffprobe; an unresolvable duration is a hard error, never a guess.
- ✔︎ ✅ 🎯 R4 cost uses one named GPU rate constant with its source cited.

Acceptance:
- [if] a cache entry has no worker.timings.container_s [then] it is absent from
  tracks_measured and contributes nothing to any sum [else ⛔️].
- [if] no entry in the window carries worker.wall_span [then] concurrency_basis
  reads 'publication-window' and observed_peak_concurrency is None [else ⛔️].
- [if] every entry carries worker.wall_span [then] peak is the sweep-line max of
  those spans and mean is time-weighted over the busy window [else ⛔️].
- [if] a measured track has no duration in cache, state.db or via ffprobe
  [then] the run exits non-zero naming the stable_id [else ⛔️].
- [if] the window contains no instrumented entries [then] exit non-zero saying so
  rather than emitting zeros [else ⛔️].
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "data" / "state" / "vocal-cache"
STATE_DB = REPO_ROOT / "data" / "state" / "state.db"

# Modal's published L4 rate, 0.80 USD per GPU-hour (modal.com/pricing, L4 24GB).
# Mirrors scripts/modal_vocal_farm.py's L4_USD_PER_S so the ledger and the farm
# price the same seconds identically.
L4_USD_PER_GPU_HOUR: float = 0.80
L4_USD_PER_S: float = L4_USD_PER_GPU_HOUR / 3600

# A gap this long between two publications means a different run, not a stall:
# within a run the farm publishes every few seconds.
RUN_GAP_S: float = 300.0

# The north star is quoted per 10 minutes of wall clock because that is about
# one "go make a coffee" iteration; per-second would be unreadable.
NORTH_STAR_WALL_S: float = 600.0

BASIS_SPANS: str = "container-wall-spans"
BASIS_PUBLICATION: str = "publication-window"


#----- samples --------------------------------------------------------------


@dataclass(frozen=True)
class TrackSample:
    """One farm-written cache entry with usable timing telemetry."""

    stable_id: str
    published_at: float  # cache-file mtime, epoch seconds
    container_s: float
    separate_s: float
    load_s: float
    duration_s: float
    wall_span: tuple[float, float] | None  # container-stamped [start, end], if persisted


def _timing(worker: dict, key: str, name: str) -> float:
    timings = worker["timings"]
    if key not in timings:
        raise ValueError(f"{name}: worker.timings missing {key!r}")
    value = timings[key]
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name}: worker.timings.{key} is not a number: {value!r}")
    return float(value)


def _wall_span(worker: dict, name: str) -> tuple[float, float] | None:
    span = worker.get("wall_span")
    if span is None:
        return None
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        raise ValueError(f"{name}: worker.wall_span is not a [start, end] pair: {span!r}")
    start, end = float(span[0]), float(span[1])
    if end < start:
        raise ValueError(f"{name}: worker.wall_span ends before it starts: {span!r}")
    return (start, end)


def _instrumented_entries(cache_dir: Path) -> list[tuple[Path, dict]]:
    """Cache files carrying container_s. Files without it predate the farm."""
    if not cache_dir.is_dir():
        raise SystemExit(f"[ERROR] vocal cache dir not found: {cache_dir}")
    found: list[tuple[Path, dict]] = []
    for path in sorted(cache_dir.glob("*.json")):
        entry = json.loads(path.read_text())
        if "container_s" in entry.get("worker", {}).get("timings", {}):
            found.append((path, entry))
    return found


def _column_by_stable_id(column: str, stable_ids: list[str]) -> dict[str, object]:
    if not stable_ids or not STATE_DB.exists():
        return {}
    marks = ",".join("?" * len(stable_ids))
    with sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            f"SELECT stable_id, {column} FROM tracks WHERE stable_id IN ({marks})",
            stable_ids,
        ).fetchall()
    return {sid: value for sid, value in rows if value}


def _ffprobe_duration_s(audio_path: str) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True, check=True,
    )
    return float(proc.stdout.strip())


def load_samples(cache_dir: Path = CACHE_DIR) -> list[TrackSample]:
    """Every instrumented cache entry, duration resolved or a hard error.

    Duration ladder is cache -> state.db -> ffprobe, in that order, because
    each step is strictly more expensive and strictly less likely to be stale.
    There is no fourth step: an unresolved duration fails the run.
    """
    entries = _instrumented_entries(cache_dir)
    if not entries:
        raise SystemExit(
            f"[ERROR] no cache entry under {cache_dir} carries worker.timings.container_s"
        )

    missing = [p.stem for p, e in entries if not e.get("duration_s")]
    from_db = {k: float(v) / 1000.0 for k, v in _column_by_stable_id("duration_ms", missing).items()}
    unprobed = [sid for sid in missing if sid not in from_db]
    from_probe = {
        sid: _ffprobe_duration_s(str(path))
        for sid, path in _column_by_stable_id("file_path", unprobed).items()
    }

    samples: list[TrackSample] = []
    unresolved: list[str] = []
    for path, entry in entries:
        stable_id = path.stem
        duration_s = entry.get("duration_s") or from_db.get(stable_id) or from_probe.get(stable_id)
        if not duration_s:
            unresolved.append(stable_id)
            continue
        worker = entry["worker"]
        samples.append(TrackSample(
            stable_id=stable_id,
            published_at=path.stat().st_mtime,
            container_s=_timing(worker, "container_s", path.name),
            separate_s=_timing(worker, "separate_s", path.name),
            load_s=_timing(worker, "load_s", path.name),
            duration_s=float(duration_s),
            wall_span=_wall_span(worker, path.name),
        ))
    if unresolved:
        raise SystemExit(
            "[ERROR] track duration unresolvable (cache, state.db and ffprobe all failed) "
            f"for {len(unresolved)} id(s): {', '.join(unresolved[:5])}"
        )
    samples.sort(key=lambda s: s.published_at)
    return samples


#----- window selection -----------------------------------------------------


def _parse_ts(raw: str) -> float:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()


def _iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_window(samples: list[TrackSample], window: str,
                  since: str | None, until: str | None) -> list[TrackSample]:
    """`latest` = last contiguous publication cluster; `all` = everything."""
    if window not in ("all", "latest"):
        raise SystemExit(f"[ERROR] --window must be 'latest' or 'all', got {window!r}")
    picked = samples
    if since is not None:
        picked = [s for s in picked if s.published_at >= _parse_ts(since)]
    if until is not None:
        picked = [s for s in picked if s.published_at <= _parse_ts(until)]
    if window == "latest" and picked:
        cut = 0
        for i in range(1, len(picked)):
            if picked[i].published_at - picked[i - 1].published_at > RUN_GAP_S:
                cut = i
        picked = picked[cut:]
    if not picked:
        raise SystemExit("[ERROR] no instrumented cache entries in the requested window")
    return picked


#----- derivation -----------------------------------------------------------


@dataclass(frozen=True)
class DerivedKpis:
    """All KPI values reconstructable from telemetry. No hand entry anywhere."""

    tracks_measured: int
    window_start: str
    window_end: str
    wall_s: float
    container_s_total: float
    per_track_wall_s: float
    separate_p50_s: float
    model_load_p50_s: float
    concurrency_basis: str
    observed_mean_concurrency: float
    observed_peak_concurrency: int | None
    cost_per_track_usd: float
    gpu_cost_total_usd: float
    stem_minutes_total: float
    stem_min_per_10min_wall: float
    tracks_cached_total: int


def sweep_peak(spans: list[tuple[float, float]]) -> int:
    """Largest number of spans overlapping at any instant.

    Ties close before they open, so a call ending exactly as another starts is
    not counted as an overlap.
    """
    events = sorted(
        [(start, 1) for start, _ in spans] + [(end, -1) for _, end in spans],
        key=lambda e: (e[0], e[1]),
    )
    peak = running = 0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    return peak


def _concurrency(samples: list[TrackSample], container_s_total: float,
                 publication_wall_s: float) -> tuple[str, float, int | None]:
    """(basis, mean, peak). Peak is None unless every call stamped its own span."""
    spans = [s.wall_span for s in samples]
    if all(span is not None for span in spans):
        real: list[tuple[float, float]] = [span for span in spans if span is not None]
        busy_s = max(end for _, end in real) - min(start for start, _ in real)
        occupied_s = sum(end - start for start, end in real)
        mean = occupied_s / busy_s if busy_s > 0 else 0.0
        return BASIS_SPANS, round(mean, 2), sweep_peak(real)
    # No container-stamped spans: publication order is not completion order, so
    # only the ratio of totals survives. See the module docstring.
    return BASIS_PUBLICATION, round(container_s_total / publication_wall_s, 2), None


def derive(samples: list[TrackSample], cache_dir: Path = CACHE_DIR) -> DerivedKpis:
    wall_s = samples[-1].published_at - samples[0].published_at
    if wall_s <= 0:
        raise SystemExit(
            "[ERROR] publication wall is zero: cannot derive rate KPIs from a single instant"
        )
    container_s_total = sum(s.container_s for s in samples)
    stem_minutes_total = sum(s.duration_s for s in samples) / 60.0
    tracks = len(samples)
    basis, mean_conc, peak_conc = _concurrency(samples, container_s_total, wall_s)
    return DerivedKpis(
        tracks_measured=tracks,
        window_start=_iso(samples[0].published_at),
        window_end=_iso(samples[-1].published_at),
        wall_s=round(wall_s, 1),
        container_s_total=round(container_s_total, 1),
        per_track_wall_s=round(wall_s / tracks, 2),
        separate_p50_s=round(statistics.median(s.separate_s for s in samples), 2),
        model_load_p50_s=round(statistics.median(s.load_s for s in samples), 2),
        concurrency_basis=basis,
        observed_mean_concurrency=mean_conc,
        observed_peak_concurrency=peak_conc,
        cost_per_track_usd=round(container_s_total * L4_USD_PER_S / tracks, 5),
        gpu_cost_total_usd=round(container_s_total * L4_USD_PER_S, 4),
        stem_minutes_total=round(stem_minutes_total, 1),
        stem_min_per_10min_wall=round(stem_minutes_total / wall_s * NORTH_STAR_WALL_S, 1),
        tracks_cached_total=len(list(cache_dir.glob("*.json"))),
    )


def derive_window(window: str = "latest", since: str | None = None,
                  until: str | None = None, cache_dir: Path = CACHE_DIR) -> DerivedKpis:
    """One-call entry point used by kpi_append.py."""
    return derive(select_window(load_samples(cache_dir), window, since, until), cache_dir)


#----- ledger mapping -------------------------------------------------------

# Ledger KPI key -> DerivedKpis field. Anything in here is DERIVED and must not
# be hand-set; anything absent (si_sdr_db, region_iou, human_quality_1to10,
# si_sdr_true_db, fixed_overhead_s, ops_incidents_per_run, the iter_latency
# pair) genuinely cannot be reconstructed from cache telemetry and stays
# hand-entered.
DERIVED_LEDGER_KEYS: dict[str, str] = {
    "per_track_wall_s": "per_track_wall_s",
    "separate_p50_s": "separate_p50_s",
    "model_load_per_track_s": "model_load_p50_s",
    "cost_per_track_usd": "cost_per_track_usd",
    "observed_mean_concurrency": "observed_mean_concurrency",
    "observed_peak_concurrency": "observed_peak_concurrency",
    "stem_min_per_10min_wall": "stem_min_per_10min_wall",
    "tracks_cached_total": "tracks_cached_total",
}


def ledger_values(derived: DerivedKpis) -> dict[str, float | None]:
    fields = asdict(derived)
    return {key: fields[attr] for key, attr in DERIVED_LEDGER_KEYS.items()}


#----- cli ------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--window", default="latest", choices=["latest", "all"],
                        help="latest contiguous publication cluster, or every entry")
    parser.add_argument("--since", default=None, help="ISO 8601 lower bound on publication time")
    parser.add_argument("--until", default=None, help="ISO 8601 upper bound on publication time")
    parser.add_argument("--cache-dir", default=str(CACHE_DIR), help="vocal-cache directory")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    derived = derive_window(args.window, args.since, args.until, Path(args.cache_dir))
    if args.json:
        print(json.dumps(asdict(derived), indent=2))
        return

    fields = asdict(derived)
    width = max(len(key) for key in fields)
    print(f"derived from {derived.tracks_measured} instrumented cache entries "
          f"({derived.window_start} .. {derived.window_end})")
    print("-" * (width + 18))
    for key, value in fields.items():
        print(f"{key.ljust(width)}  {'not derivable' if value is None else value}")
    print("-" * (width + 18))
    if derived.concurrency_basis == BASIS_PUBLICATION:
        print("NOTE: no container-stamped wall_span in this window, so mean concurrency is "
              "GPU-seconds per second of PUBLICATION wall and peak is not derivable.")
    print(f"NORTH STAR: {derived.stem_min_per_10min_wall} stem-minutes separated "
          f"per 10 minutes of wall clock")


if __name__ == "__main__":
    main()
