#!/usr/bin/env python3
"""Bounded read-ahead for a farm's input pipeline.

WHY THIS EXISTS (the bug it fixes):

Modal's SYNC ``.map()`` / ``.starmap()`` does not run its event loop on a
background thread. ``modal/_utils/async_utils.py::run_async_gen`` drives the
loop with ``runner.run(gen.asend(...))`` on the CALLING thread, one step per
output, and ``sync_or_async_iter`` pulls a sync input iterator inline -- its own
comment says "This intentionally could block the event loop for the duration of
calling __iter__ and __next__".

So a feeder generator that calls ``read_bytes()`` per item stops Modal's blob
uploads, input pumping AND output fetching for the whole duration of that read.
An evicted network-backed file can block while materialising on first read,
so a serial feeder can constrain container fan-out: Modal's 20-way upload parallelism
(``parallel_map.py``, ``concurrency=BLOB_MAX_PARALLELISM``) can never hold more
than the single item its starved source has produced.

Reading ahead on a thread pool means the generator handed to ``.starmap()``
almost always returns an ALREADY-BUFFERED item, so the event loop stalls for a
dict lookup rather than for a network-backed file read.

Both bounds are enforced on the CONSUMER thread, never inside a pool thread, so
a worker can never block on backpressure and abandoning the generator can never
deadlock the pool's shutdown.

  ✔︎ ✅ 🎯 items are yielded in input order, one per input path.
    [if] paths are [a, b, c] [then] yields describe a, then b, then c
    [if] a read raises [then ⛔️] it propagates at that item's position
  ✔︎ ✅ 🎯 at most ``depth`` reads are in flight and at most ``max_bytes`` of
    file bytes are claimed by the window.
    [if] the next file would exceed max_bytes [then] it is not submitted until
    an earlier item is consumed
    [if] one file alone exceeds max_bytes [then] it still runs, alone, rather
    than deadlocking
  ✔︎ ✅ 🎯 the stat used for a source-file signature is taken in the same
    thread immediately AFTER that file's read, so it describes the generation
    whose bytes were actually sent.
    [if] a file is materialised by our own read [then] the stat sees the new
    inode, not the placeholder's

-Claude
"""
from __future__ import annotations

import os
from collections import deque
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

# Read concurrency overlaps network-backed materialisation waits rather than
# CPU work. The private tuning corpus and timing results are not included
# in the public source; these defaults are configuration, not a public
# throughput acceptance claim.
DEFAULT_DEPTH: int = 32
DEFAULT_WORKERS: int = 16
# The byte ceiling bounds memory independently of depth, which only bounds
# the item count. Large files must not silently multiply the resident set.
DEFAULT_MAX_BYTES: int = 512_000_000


@dataclass(frozen=True)
class ReadyFile:
    """One file read into memory, with the stat taken right after the read."""

    path: Path
    data: bytes
    stat: os.stat_result


def _read_one(path: Path) -> ReadyFile:
    """Read the bytes, THEN stat.

    Order matters: reading an evicted network-backed file can materialise it
    and assign a new inode. A stat taken before that read can describe a
    generation that no longer exists when its bytes are published. Take the
    signature after the read so the publication guard checks that generation.
    """
    data = path.read_bytes()
    return ReadyFile(path=path, data=data, stat=path.stat())


def read_ahead(
    paths: Iterable[Path],
    *,
    depth: int = DEFAULT_DEPTH,
    workers: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Iterator[ReadyFile]:
    """Yield each path's bytes in input order, reading ahead on a thread pool.

    ``depth`` caps reads in flight; ``max_bytes`` caps the file bytes those
    reads have claimed. A file bigger than the whole budget is still read, on
    its own, rather than stalling forever.
    """
    if depth < 1:
        raise ValueError(f"depth must be >= 1, got {depth}")
    if workers is None:
        from apps.shared.app_posture import apply_posture_to_workers
        from apps.shared.perf_tier import background_worker_count

        workers = apply_posture_to_workers(background_worker_count(DEFAULT_WORKERS))
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    if max_bytes < 1:
        raise ValueError(f"max_bytes must be >= 1, got {max_bytes}")

    upcoming = iter(paths)
    window: deque[tuple[Future[ReadyFile], int]] = deque()
    claimed = 0

    def _next_sized() -> tuple[Path, int] | None:
        """Next path plus its size. The stat is metadata-only, so it is cheap
        even for an evicted file whose bytes are not on disk."""
        path = next(upcoming, None)
        if path is None:
            return None
        return path, path.stat().st_size

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prefetch") as pool:
        pending = _next_sized()
        while pending is not None or window:
            while pending is not None and len(window) < depth:
                path, size = pending
                # An empty window always admits one item, so a single file
                # larger than the whole budget makes progress alone.
                if window and claimed + size > max_bytes:
                    break
                window.append((pool.submit(_read_one, path), size))
                claimed += size
                pending = _next_sized()
            future, size = window.popleft()
            item = future.result()
            claimed -= size
            yield item
