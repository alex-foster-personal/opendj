"""Time server-side row hydration (``get_tracks_bulk`` + ``build_track_rows``) per row on the synthetic library.

Mini-PRD
--------
The playlist pane fill at 10k is over Acceptable and ``(private path)`` puts what
remains on per-row hydration (about 0.34 ms per row). This command measures that cost in isolation
so a fix can show a delta. Target for the follow-up fix: 0.17 ms per row.

Requirements:
- [ ] [if] ``--tracks 200 --repeats 2 --json`` runs on a temp dir [then] it prints
  ``per_row_ms_median``, ``per_row_ms_p95`` and ``rows`` = 200 and exits 0.
- [ ] [if] ``--profile`` is passed [then] the output adds ``profile_top``: 10 entries naming
  functions and their cumulative seconds.
- [ ] [if] the bench runs with ``OPENDJ_DATA_DIR`` set or unset [then] that dir is unchanged:
  the fixture lives in a fresh temp dir and ``OPENDJ_DATA_DIR`` points there for the run.

Each repeat hydrates every page of ``--page-size`` stable ids (500, the playlist page) in
playlist order. A page's per-row cost is its wall time divided by its row count; median and p95
are over all pages of all repeats.

Usage::

    uv run python -m scripts.perf.row_hydration_bench --tracks 10000 --json
"""

from __future__ import annotations

import argparse
import contextlib
import cProfile
import json
import os
import pstats
import statistics
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from apps.webui.server import rb_vendor
from apps.webui.server.sqlite_backend import SqliteBackend
from scripts.perf.generate_library_scale_fixture import build

DEFAULT_TRACKS = 10_000
DEFAULT_SEED = 2328
DEFAULT_REPEATS = 3
PAGE_SIZE = 500
PROFILE_TOP_N = 10
DATA_DIR_ENV = "OPENDJ_DATA_DIR"


@dataclass(frozen=True)
class BenchResult:
    tracks: int
    seed: int
    repeats: int
    page_size: int
    rows: int
    pages: int
    per_row_ms_median: float
    per_row_ms_p95: float
    profile_top: list[dict[str, Any]] = field(default_factory=list)


# ----- helpers


@contextlib.contextmanager
def _isolated_data_dir() -> Iterator[Path]:
    """A fresh temp data dir with ``OPENDJ_DATA_DIR`` pointing at it, restored afterwards."""
    previous = os.environ.get(DATA_DIR_ENV)
    with tempfile.TemporaryDirectory(prefix="row-hydration-bench-") as raw:
        data_dir = Path(raw).resolve() / "library"
        os.environ[DATA_DIR_ENV] = os.fspath(data_dir)
        try:
            yield data_dir
        finally:
            if previous is None:
                os.environ.pop(DATA_DIR_ENV, None)
            elif previous is not None:
                os.environ[DATA_DIR_ENV] = previous


def _playlist_stable_ids(backend: SqliteBackend, playlist_id: str, total: int) -> list[str]:
    page = backend.get_playlist_page(playlist_id, limit=total, offset=0)
    return list(page.playlist.items)


def _hydrate_page(backend: SqliteBackend, stable_ids: list[str], data_dir: Path, state_db: Path) -> int:
    tracks_map = backend.get_tracks_bulk(stable_ids)
    missing = [sid for sid in stable_ids if sid not in tracks_map]
    if missing:
        raise RuntimeError(f"{len(missing)} playlist members have no track row (first: {missing[:3]})")
    rows = rb_vendor.build_track_rows([tracks_map[sid] for sid in stable_ids], data_dir=data_dir, state_db_path=state_db)
    return len(rows)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _profile_top(profiler: cProfile.Profile) -> list[dict[str, Any]]:
    stats = pstats.Stats(profiler)
    entries = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)  # type: ignore[attr-defined]
    return [
        {"function": f"{Path(filename).name}:{line}({name})", "cumulative_s": round(cumulative, 6)}
        for (filename, line, name), (_cc, _nc, _tt, cumulative, _callers) in entries[:PROFILE_TOP_N]
    ]


# ----- bench


def run_bench(*, tracks: int, seed: int, repeats: int, page_size: int, profile: bool) -> BenchResult:
    """Build the synthetic library in a temp dir and time hydration per row over ``repeats`` passes."""
    if repeats < 1 or page_size < 1:
        raise ValueError("--repeats and --page-size must be at least 1")
    with _isolated_data_dir() as data_dir:
        build(data_dir, tracks=tracks, seed=seed)
        state_db = data_dir / "state" / "state.db"
        backend = SqliteBackend(state_db)
        stable_ids = _playlist_stable_ids(backend, f"libm120-scale-{tracks}", tracks)
        if len(stable_ids) != tracks:
            raise RuntimeError(f"fixture playlist has {len(stable_ids)} members, expected {tracks}")
        pages = [stable_ids[start : start + page_size] for start in range(0, tracks, page_size)]
        profiler = cProfile.Profile() if profile else None
        per_row_ms: list[float] = []
        rows_per_repeat = 0
        for _ in range(repeats):
            rows_per_repeat = 0
            for page in pages:
                if profiler is not None:
                    profiler.enable()
                started = time.perf_counter()
                hydrated = _hydrate_page(backend, page, data_dir, state_db)
                elapsed_ms = (time.perf_counter() - started) * 1_000
                if profiler is not None:
                    profiler.disable()
                if hydrated != len(page):
                    raise RuntimeError(f"build_track_rows returned {hydrated} rows for a page of {len(page)}")
                rows_per_repeat += hydrated
                per_row_ms.append(elapsed_ms / hydrated)
    return BenchResult(
        tracks=tracks,
        seed=seed,
        repeats=repeats,
        page_size=page_size,
        rows=rows_per_repeat,
        pages=len(pages),
        per_row_ms_median=round(statistics.median(per_row_ms), 6),
        per_row_ms_p95=round(_percentile(per_row_ms, 0.95), 6),
        profile_top=_profile_top(profiler) if profiler is not None else [],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tracks", type=int, default=DEFAULT_TRACKS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE)
    parser.add_argument("--profile", action="store_true", help="add a cProfile top-10 by cumulative time")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    result = run_bench(
        tracks=args.tracks, seed=args.seed, repeats=args.repeats, page_size=args.page_size, profile=args.profile
    )
    if args.json:
        print(json.dumps(asdict(result), sort_keys=True))
        return 0
    print(
        f"[OK] rows={result.rows} pages={result.pages} repeats={result.repeats} "
        f"per_row_ms median={result.per_row_ms_median:.4f} p95={result.per_row_ms_p95:.4f}"
    )
    for entry in result.profile_top:
        print(f"  {entry['cumulative_s']:>10.4f}s  {entry['function']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
