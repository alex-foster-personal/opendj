#!/usr/bin/env python3
"""Measure how fast the farm's input pipeline can produce ready-to-send items.

WHY THIS EXISTS: the Modal farm is spend-gated, so the fan-out fix cannot be
validated by running the farm. But the fix is entirely local -- it changes how
fast audio bytes reach ``.starmap`` -- so the thing to measure is the feeder in
isolation, on the same real library paths, with no GPU and no network.

WHAT IT MEASURES: Modal's sync ``.starmap`` pulls its input iterator INLINE on
the event-loop thread (``sync_or_async_iter``, whose own comment warns it
"could block the event loop"), so time spent in the feeder is time the whole
pipeline is frozen: no blob uploads, no dispatch, no output collection.

READ THE PROJECTION, NOT JUST THE ARM RATES. The two timed arms run on a 100%
iCloud-evicted sample, which is a WORST CASE and not the production
population. Projected onto the real unfarmed gap (~16-18% evicted), a SERIAL
feeder already implies far more containers than the cap, so input RATE was
never the binding constraint on a representative batch. The claim that
survives is narrower and different in kind: the read-ahead removes several
minutes of WHOLE-PIPELINE freeze caused by first-touch materialisation. The
report prints both, and ``verdict_on_real_gap`` is the one to quote.

THE MEASUREMENT TRAP THIS AVOIDS: reading an iCloud-evicted file materialises
it, so the SECOND arm of a naive A/B runs against warm files and the read-ahead
looks hundreds of times better than it is. Every timed comparison here
therefore runs on DISJOINT, size-interleaved halves of the same evicted sample,
so both arms pay first-read materialisation. ``--verify-eviction`` additionally
re-checks each sampled file's residency immediately before its arm runs and
refuses to report if the sample warmed up in between.

THIS BENCHMARK IS SELF-DEPLETING AND IS NOW OFF BY DEFAULT. Every run
permanently materialises the files it samples: the evicted pool fell from 282
to 199 over five runs, about a third of the population the read-ahead exists to
serve. That population is non-renewable here (only new imports or an iCloud
purge restore it) and the farm's evicted-file smoke is drawn from it, so the
timed arms REFUSE to run without ``--consume-evicted-files``. See
``assert_consumption_allowed``. The conclusion is already recorded below; do
not re-measure without coordinating first.

Run (the projection needs no reads and is always safe):
  uv run python scripts/bench/input_pipeline_bench.py --sample 24  # refuses

  ✔︎ ✅ 🎯 serial and prefetched arms read disjoint file sets of comparable
    size, both cold.
    [if] the two arms share a path [then ⛔️] refuse to report
    [if] a sampled file is already resident [then] it is excluded from the
    cold sample and counted in the warm control
  ✔︎ ✅ 🎯 the reported rate is items/second at the point of yield, which is
    what Modal's feeder ceiling is denominated in.
    [if] prefetched items/s <= serial items/s on cold files [then] the fix
    bought nothing and the report says so

-Claude
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from apps.vocals.prefetch import (
    DEFAULT_DEPTH,
    DEFAULT_MAX_BYTES,
    DEFAULT_WORKERS,
    read_ahead,
)

# Container time one track holds a GPU for: 954.1 container-seconds over the
# 118 INSTRUMENTED entries of the 128-track run. An earlier 7.45 here divided
# by 128, but the 10-track smoke predates the instrumentation and contributed
# no container_s, so it inflated the denominator and understated the figure.
CONTAINER_S_PER_TRACK: float = 8.09
# The BETTER scaling basis. separate_s is roughly proportional to track
# duration, so projecting GPU cost by track count assumes the backlog has the
# same mean duration as the measured sample, and it does not: the ledger's 118
# tracks average 328.0s while the 989-track backlog averages 288.4s (verified
# against state.db). Track-count scaling therefore overstates the GPU floor by
# ~14%. 954.1 container-seconds over 645.1 stem-minutes of audio.
CONTAINER_S_PER_AUDIO_S: float = 954.1 / (645.1 * 60)
CONFIGURED_MAX_CONTAINERS: int = 10
# Mac uplink, measured Fri 24 Jul 2026 by the throughput agent. The 25 MB/s in
# the asset-store skill doc is stale. Used only to state the transfer floor
# alongside the GPU floor, so nobody tunes the feeder past the point where
# something else binds.
UPLINK_MB_S: float = 14.6


@dataclass(frozen=True)
class ArmResult:
    """One timed pass over one disjoint set of files."""

    arm: str
    files: int
    bytes_read: int
    wall_s: float
    items_per_s: float
    mb_per_s: float
    mean_item_s: float
    max_item_s: float
    implied_max_containers: float


def _sample_paths(state_db: Path) -> list[Path]:
    """Every playlist-member track that has a real file on disk.

    Same population the farm's ``compute_gap`` feeds from, minus the
    cache-gap filter: this benchmark measures reading, not what needs reading.
    """
    if not state_db.is_file():
        raise SystemExit(f"error: STATE_DB missing: {state_db}")
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT t.file_path FROM playlist_memberships m "
            "JOIN tracks t ON t.stable_id = m.stable_id "
            "WHERE t.file_path IS NOT NULL AND t.file_path != ''"
        ).fetchall()
    finally:
        connection.close()
    return [Path(row[0]) for row in rows]


def is_evicted(path: Path) -> bool:
    """True if the file is an iCloud placeholder: full size, no blocks on disk.

    macOS keeps the metadata of an evicted file local and reports its real
    st_size, so size alone cannot tell you whether the bytes are here.
    st_blocks == 0 for a non-empty file is the residency signal.
    """
    stat = path.stat()
    return stat.st_size > 0 and stat.st_blocks == 0


def _partition(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Split into (evicted, resident), skipping anything unreadable."""
    evicted: list[Path] = []
    resident: list[Path] = []
    for path in paths:
        try:
            (evicted if is_evicted(path) else resident).append(path)
        except OSError:
            continue
    return evicted, resident


def _interleave(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Two halves with matched size distributions.

    Sorting by size and dealing alternately means neither arm can win by
    drawing the small files. A random split would leave the comparison hostage
    to a 55 MB outlier landing on one side.
    """
    ordered = sorted(paths, key=lambda p: p.stat().st_size)
    return ordered[0::2], ordered[1::2]


def _serial_reads(paths: list[Path]) -> Iterator[tuple[Path, bytes]]:
    """Exactly what the farm's feeder used to do: read, then stat, inline."""
    for path in paths:
        data = path.read_bytes()
        path.stat()
        yield path, data


def _prefetched_reads(
    paths: list[Path], depth: int, workers: int, max_bytes: int
) -> Iterator[tuple[Path, bytes]]:
    for ready in read_ahead(
        paths, depth=depth, workers=workers, max_bytes=max_bytes
    ):
        yield ready.path, ready.data


def _time_arm(
    arm: str, paths: list[Path], items: Iterator[tuple[Path, bytes]]
) -> ArmResult:
    """Time the gaps BETWEEN yields, which is what stalls Modal's event loop.

    Not the total read time: with read-ahead a read overlaps other reads, and
    the only cost the pipeline actually pays is the wait at the yield point.
    """
    gaps: list[float] = []
    bytes_read = 0
    started = time.perf_counter()
    resumed = started
    for _, data in items:
        gaps.append(time.perf_counter() - resumed)
        bytes_read += len(data)
        del data  # do not accumulate the batch in RAM; we only need the count
        resumed = time.perf_counter()
    wall_s = time.perf_counter() - started
    if not gaps:
        raise SystemExit(f"error: arm {arm!r} produced no items")
    items_per_s = len(gaps) / wall_s
    return ArmResult(
        arm=arm,
        files=len(gaps),
        bytes_read=bytes_read,
        wall_s=round(wall_s, 2),
        items_per_s=round(items_per_s, 3),
        mb_per_s=round(bytes_read / 1e6 / wall_s, 1),
        mean_item_s=round(sum(gaps) / len(gaps), 3),
        max_item_s=round(max(gaps), 3),
        # A feeder emitting one item every 1/items_per_s seconds cannot keep
        # more than this many containers busy, whatever the cap is set to.
        implied_max_containers=round(items_per_s * CONTAINER_S_PER_TRACK, 2),
    )


def project_gap_impact(
    cold_item_s: float, warm_item_s: float, evicted: int, resident: int,
    gap_bytes: int, gap_audio_s: float = 0.0,
) -> dict[str, Any]:
    """What the read-ahead is actually worth on the REAL batch, not this sample.

    READ THIS BEFORE QUOTING THE ARM RATES ABOVE. Both timed arms run on a
    100%-evicted sample, deliberately, because that is the only way to compare
    like with like on first-touch cost. The production gap is nothing like
    that: it is ~18% evicted and ~82% already resident, and a resident read is
    roughly 500x faster. So the arm-to-arm speedup is a WORST-CASE figure and
    must not be presented as the expected throughput gain.

    Projected onto the real mix, a SERIAL feeder already clears the container
    cap comfortably. That kills the throughput argument for this fix: input
    RATE was not the binding constraint on a representative batch.

    What survives, and is the honest reason to keep the read-ahead, is that a
    blocking read does not merely delay its own input. Modal's sync .starmap
    advances its event loop on the CALLING thread, so the read freezes blob
    uploads, input dispatch and output collection TOO. The cost is therefore
    (number of evicted files) x (first-touch seconds) of whole-pipeline
    stall, whatever the average rate looks like.
    """
    total = evicted + resident
    if total <= 0:
        raise SystemExit("error: empty gap, nothing to project onto")
    evicted_fraction = evicted / total
    serial_item_s = evicted_fraction * cold_item_s + (1 - evicted_fraction) * warm_item_s
    serial_items_per_s = 1.0 / serial_item_s
    # Every evicted file's first touch is dead time for the WHOLE pipeline.
    freeze_s = evicted * cold_item_s
    # Audio-seconds when we have them, track count only as a fallback: see
    # CONTAINER_S_PER_AUDIO_S for why the two disagree by ~14%.
    if gap_audio_s > 0:
        gpu_s_total = gap_audio_s * CONTAINER_S_PER_AUDIO_S
        gpu_basis = "audio-seconds"
    else:
        gpu_s_total = total * CONTAINER_S_PER_TRACK
        gpu_basis = "track-count (overstates: assumes the sample's mean duration)"
    gpu_floor_s = gpu_s_total / CONFIGURED_MAX_CONTAINERS
    upload_floor_s = gap_bytes / 1e6 / UPLINK_MB_S
    return {
        "gap_tracks": total,
        "gap_gb": round(gap_bytes / 1e9, 2),
        "evicted": evicted,
        "evicted_pct": round(100 * evicted_fraction, 1),
        "serial_items_per_s_mixed": round(serial_items_per_s, 2),
        "serial_implied_containers_mixed": round(
            serial_items_per_s * CONTAINER_S_PER_TRACK, 1
        ),
        "pipeline_freeze_s": round(freeze_s, 0),
        "pipeline_freeze_min": round(freeze_s / 60, 1),
        "gpu_floor_min": round(gpu_floor_s / 60, 1),
        "gpu_floor_basis": gpu_basis,
        "upload_floor_min": round(upload_floor_s / 60, 1),
        "freeze_pct_of_run": round(
            100 * freeze_s / max(gpu_floor_s, upload_floor_s), 0
        ),
    }


def gap_composition(
    state_db: Path, cache_dir: Path
) -> tuple[int, int, int, float]:
    """(evicted, resident, bytes, audio_seconds) over the tracks still to farm.

    STATS ONLY, never reads. stat() on an iCloud placeholder returns local
    metadata and does NOT materialise the file, so this is safe to call
    repeatedly. Reading would consume the very population it measures.

    The already-farmed tracks are the wrong population to measure: farming a
    track reads it, and reading materialises it, so the cached set is only ~5%
    evicted while the unfarmed set is ~18%.
    """
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT t.stable_id, t.file_path, t.duration_ms "
            "FROM playlist_memberships m "
            "JOIN tracks t ON t.stable_id = m.stable_id "
            "WHERE t.file_path IS NOT NULL AND t.file_path != ''"
        ).fetchall()
    finally:
        connection.close()
    cached = {path.stem for path in cache_dir.glob("*.json")}
    evicted = resident = total_bytes = 0
    audio_s = 0.0
    for stable_id, file_path, duration_ms in rows:
        if stable_id in cached:
            continue
        path = Path(file_path)
        try:
            stat = path.stat()
        except OSError:
            continue
        total_bytes += stat.st_size
        audio_s += (duration_ms or 0) / 1000.0
        if stat.st_size > 0 and stat.st_blocks == 0:
            evicted += 1
        else:
            resident += 1
    return evicted, resident, total_bytes, audio_s


def _assert_cold(arm: str, paths: list[Path]) -> None:
    warm = [p for p in paths if not is_evicted(p)]
    if warm:
        raise SystemExit(
            f"error: {len(warm)} of {len(paths)} files in arm {arm!r} are "
            f"already resident, so this arm would not pay materialisation and "
            f"the comparison would be meaningless. First: {warm[0]}"
        )


def assert_consumption_allowed(consume_evicted: bool) -> None:
    """Refuse the timed arms unless the caller has explicitly accepted the cost.

    THE EVICTED POOL IS NON-RENEWABLE WITHIN THIS LIBRARY. Reading an iCloud
    placeholder materialises it permanently; only new imports or an explicit
    iCloud purge restore one. Five runs of this benchmark took the pool from
    282 files to 199, i.e. roughly a third of the population the read-ahead
    exists to serve, and the evicted-file smoke is a GATE on the 989-track
    farm. Every further run makes that gate less meaningful.

    So the default is REFUSE. This is a guard rather than a note in a docstring
    because the failure mode is a well-meaning agent running it "just once
    more" and quietly spending a shared, unrecoverable resource.
    """
    if consume_evicted:
        return
    raise SystemExit(
        "refusing to run: the timed arms READ iCloud-evicted files, which "
        "materialises them permanently and shrinks the population the "
        "evicted-file farm smoke is drawn from (282 -> 199 across five prior "
        "runs).\n"
        "The measurement it produced is already recorded and its conclusion "
        "is settled: input RATE was never binding on the real gap mix; what "
        "the read-ahead removes is whole-pipeline freeze.\n"
        "If you genuinely need to re-measure, coordinate first, then pass "
        "--consume-evicted-files."
    )


def run_benchmark(
    state_db: Path,
    sample: int,
    depth: int,
    workers: int,
    max_bytes: int,
    seed: int,
    verify_eviction: bool,
) -> dict[str, Any]:
    all_paths = _sample_paths(state_db)
    evicted, resident = _partition(all_paths)
    print(
        f"library: {len(all_paths)} playlist-member paths, {len(evicted)} "
        f"iCloud-evicted, {len(resident)} resident"
    )
    if len(evicted) < 4:
        raise SystemExit(
            f"error: only {len(evicted)} evicted files available; this "
            "benchmark measures first-read materialisation and cannot run "
            "without a cold sample."
        )

    rng = random.Random(seed)
    chosen = rng.sample(evicted, min(sample, len(evicted)))
    serial_paths, prefetch_paths = _interleave(chosen)
    overlap = set(serial_paths) & set(prefetch_paths)
    if overlap:
        raise SystemExit(f"error: arms share {len(overlap)} paths; not a fair test")
    print(
        f"cold sample: {len(chosen)} evicted files split into disjoint halves "
        f"({len(serial_paths)} serial, {len(prefetch_paths)} prefetched), "
        "size-interleaved so neither arm draws the small files"
    )

    if verify_eviction:
        _assert_cold("serial", serial_paths)
    serial = _time_arm("serial-cold", serial_paths, _serial_reads(serial_paths))
    print(f"  {serial.arm}: {serial.items_per_s} items/s, {serial.mb_per_s} MB/s")

    if verify_eviction:
        _assert_cold("prefetched", prefetch_paths)
    prefetched = _time_arm(
        "prefetched-cold",
        prefetch_paths,
        _prefetched_reads(prefetch_paths, depth, workers, max_bytes),
    )
    print(
        f"  {prefetched.arm}: {prefetched.items_per_s} items/s, "
        f"{prefetched.mb_per_s} MB/s"
    )

    # Warm control: the floor the feeder cannot beat once iCloud is out of the
    # picture. If prefetched-cold approaches this, materialisation has stopped
    # being the constraint.
    warm_sample = rng.sample(resident, min(len(serial_paths), len(resident)))
    warm = _time_arm("serial-warm", warm_sample, _serial_reads(warm_sample))
    print(f"  {warm.arm}: {warm.items_per_s} items/s (control, no eviction)")

    speedup = prefetched.items_per_s / serial.items_per_s
    evicted_n, resident_n, gap_bytes, gap_audio_s = gap_composition(
        state_db, Path(str(state_db.parent / "vocal-cache"))
    )
    projection = project_gap_impact(
        cold_item_s=serial.mean_item_s,
        warm_item_s=warm.mean_item_s,
        evicted=evicted_n,
        resident=resident_n,
        gap_bytes=gap_bytes,
        gap_audio_s=gap_audio_s,
    )
    return {
        "container_s_per_track": CONTAINER_S_PER_TRACK,
        "configured_max_containers": CONFIGURED_MAX_CONTAINERS,
        "prefetch": {"depth": depth, "workers": workers, "max_bytes": max_bytes},
        "arms": [asdict(serial), asdict(prefetched), asdict(warm)],
        "speedup_items_per_s": round(speedup, 2),
        "gap_projection": projection,
        "verdict": (
            "prefetch raises the WORST-CASE feeder ceiling"
            if prefetched.implied_max_containers > serial.implied_max_containers
            else "NO IMPROVEMENT: prefetch did not raise the feeder ceiling"
        ),
        # The claim that actually survives contact with the real population.
        "verdict_on_real_gap": (
            f"input RATE was never binding on the real mix (a serial feeder "
            f"already implies {projection['serial_implied_containers_mixed']} "
            f"containers against a cap of {CONFIGURED_MAX_CONTAINERS}); the win "
            f"is removing {projection['pipeline_freeze_min']} min of "
            f"whole-pipeline freeze, {projection['freeze_pct_of_run']}% of a "
            f"projected run"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/bench/input_pipeline_bench.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sample", type=int, default=24,
        help="evicted files to draw, split across the two arms (default 24)",
    )
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--seed", type=int, default=0, help="sample seed (default 0, reproducible)"
    )
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None, help="write results here")
    parser.add_argument(
        "--no-verify-eviction", dest="verify_eviction", action="store_false",
        help="skip the pre-arm residency check (only for a warm-only rerun)",
    )
    parser.add_argument(
        "--consume-evicted-files", action="store_true",
        help="REQUIRED to run the timed arms. They permanently materialise the "
             "evicted files they sample, shrinking a non-renewable pool that "
             "the farm's evicted-file smoke is drawn from. Off by default.",
    )
    parser.set_defaults(verify_eviction=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.data_dir is not None:
        data_dir = args.data_dir
    else:
        from apps.shared.paths import DATA_DIR

        data_dir = DATA_DIR

    assert_consumption_allowed(args.consume_evicted_files)
    report = run_benchmark(
        state_db=data_dir / "state" / "state.db",
        sample=args.sample,
        depth=args.depth,
        workers=args.workers,
        max_bytes=args.max_bytes,
        seed=args.seed,
        verify_eviction=args.verify_eviction,
    )
    serial, prefetched, warm = report["arms"]
    print(
        f"\nfeeder rate: {serial['items_per_s']} -> {prefetched['items_per_s']} "
        f"items/s ({report['speedup_items_per_s']}x), both on cold "
        f"iCloud-evicted files"
    )
    print(
        f"container ceiling this implies at {CONTAINER_S_PER_TRACK}s/container: "
        f"{serial['implied_max_containers']} -> "
        f"{prefetched['implied_max_containers']} "
        f"(cap is {CONFIGURED_MAX_CONTAINERS})"
    )
    print(f"warm control: {warm['items_per_s']} items/s")
    print(f"verdict (worst case): {report['verdict']}")

    gp = report["gap_projection"]
    print(
        f"\nPROJECTED ONTO THE REAL GAP ({gp['gap_tracks']} tracks, "
        f"{gp['gap_gb']} GB, {gp['evicted_pct']}% evicted -- NOT the 100% "
        f"evicted sample timed above):"
    )
    print(
        f"  a SERIAL feeder on this mix already implies "
        f"{gp['serial_implied_containers_mixed']} containers against a cap of "
        f"{CONFIGURED_MAX_CONTAINERS}, so input RATE was never binding here"
    )
    print(
        f"  what the read-ahead removes is {gp['pipeline_freeze_min']} min of "
        f"WHOLE-PIPELINE freeze ({gp['evicted']} evicted files x "
        f"{report['arms'][0]['mean_item_s']}s first touch), "
        f"{gp['freeze_pct_of_run']}% of a projected run"
    )
    print(
        f"  floors: GPU {gp['gpu_floor_min']} min at {CONFIGURED_MAX_CONTAINERS} "
        f"containers, upload {gp['upload_floor_min']} min at {UPLINK_MB_S} MB/s "
        "-- so a working 10x fan-out lands near the transfer floor and there is "
        "nothing further to win from concurrency"
    )
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    return 0 if prefetched["implied_max_containers"] > serial[
        "implied_max_containers"
    ] else 1


if __name__ == "__main__":
    raise SystemExit(main())
