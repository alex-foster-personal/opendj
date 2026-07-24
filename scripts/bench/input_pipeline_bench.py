#!/usr/bin/env python3
"""Measure how fast the farm's input pipeline can produce ready-to-send items.

WHY THIS EXISTS: the Modal farm is spend-gated, so the fan-out fix cannot be
validated by running the farm. But the fix is entirely local -- it changes how
fast audio bytes reach ``.starmap`` -- so the thing to measure is the feeder in
isolation, on the same real library paths, with no GPU and no network.

WHAT IT PROVES: Modal's sync ``.starmap`` pulls its input iterator INLINE on
the event-loop thread (``sync_or_async_iter``, whose own comment warns it
"could block the event loop"), so the feeder's items-per-second is a hard
ceiling on container concurrency. If a container holds a GPU for C seconds and
the feeder emits one item every F seconds, no more than C/F containers can ever
be busy at once. Raising the feeder's rate raises that ceiling directly.

THE MEASUREMENT TRAP THIS AVOIDS: reading an iCloud-evicted file materialises
it, so the SECOND arm of a naive A/B runs against warm files and the read-ahead
looks 10x better than it is. Every timed comparison here therefore runs on
DISJOINT, size-interleaved halves of the same evicted sample, so both arms pay
first-read materialisation. ``--verify-eviction`` additionally re-checks each
sampled file's residency immediately before its arm runs and refuses to report
if the sample warmed up in between.

  ✔︎ ✅ 🎯 serial and prefetched arms read disjoint file sets of comparable
    size, both cold.
    [if] the two arms share a path [then ⛔️] refuse to report
    [if] a sampled file is already resident [then] it is excluded from the
    cold sample and counted in the warm control
  ✔︎ ✅ 🎯 the reported rate is items/second at the point of yield, which is
    what Modal's feeder ceiling is denominated in.
    [if] prefetched items/s <= serial items/s on cold files [then] the fix
    bought nothing and the report says so

Run:
  uv run python scripts/bench/input_pipeline_bench.py --sample 24
  uv run python scripts/bench/input_pipeline_bench.py --sample 24 --json out.json

-Claude
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from apps.vocals.prefetch import (
    DEFAULT_DEPTH,
    DEFAULT_MAX_BYTES,
    DEFAULT_WORKERS,
    read_ahead,
)

# The container time one track holds a GPU for, measured over the 128-track
# production run (954 container-seconds / 128 tracks). Used only to turn a
# feeder rate into the container-concurrency ceiling it implies.
CONTAINER_S_PER_TRACK: float = 7.45
CONFIGURED_MAX_CONTAINERS: int = 10


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


def _assert_cold(arm: str, paths: list[Path]) -> None:
    warm = [p for p in paths if not is_evicted(p)]
    if warm:
        raise SystemExit(
            f"error: {len(warm)} of {len(paths)} files in arm {arm!r} are "
            f"already resident, so this arm would not pay materialisation and "
            f"the comparison would be meaningless. First: {warm[0]}"
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
    return {
        "container_s_per_track": CONTAINER_S_PER_TRACK,
        "configured_max_containers": CONFIGURED_MAX_CONTAINERS,
        "prefetch": {"depth": depth, "workers": workers, "max_bytes": max_bytes},
        "arms": [asdict(serial), asdict(prefetched), asdict(warm)],
        "speedup_items_per_s": round(speedup, 2),
        "verdict": (
            "prefetch raises the feeder ceiling"
            if prefetched.implied_max_containers > serial.implied_max_containers
            else "NO IMPROVEMENT: prefetch did not raise the feeder ceiling"
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
    parser.set_defaults(verify_eviction=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.data_dir is not None:
        data_dir = args.data_dir
    else:
        from apps.shared.paths import DATA_DIR

        data_dir = DATA_DIR

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
    print(f"verdict: {report['verdict']}")
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    return 0 if prefetched["implied_max_containers"] > serial[
        "implied_max_containers"
    ] else 1


if __name__ == "__main__":
    raise SystemExit(main())
