"""Real-library link-repair tests. Ties to RECON-01.

Split from ``test_link_repair.py`` (its synthetic half): these check the
bucket-sum invariant and the read-only guarantee against the actual
``data/state/state.db``, and skip cleanly when that file is absent (fresh
checkout, CI, cloud sandbox). Point a worktree at the primary checkout's
library with ``MDT_DATA_DIR=/path/to/data pytest``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from apps.reconcile import index_disk, match
from apps.shared import paths
from tests.reconcile.conftest import HAS_REAL_LIBRARY, HAS_REAL_SCHEMA


def _real_data_dir() -> Path:
    env = os.environ.get("MDT_DATA_DIR")
    return Path(env) if env else paths.DATA_DIR


DATA_DIR: Path = _real_data_dir()
STATE_DB: Path = DATA_DIR / "state" / "state.db"
RB_DB: Path = DATA_DIR / "master.plain.db"
INDEX_CACHE: Path = DATA_DIR / "state" / "disk-audio-index.json"

_SKIP_REASON = "real library not present (state.db missing or has zero track rows)"
_SKIP_SCHEMA_REASON = "real state.db not present (missing; an unreadable one fails collection)"


@pytest.mark.skipif(not HAS_REAL_SCHEMA, reason=_SKIP_SCHEMA_REASON)
@pytest.mark.requirement("RECON-01")
def test_real_state_db_is_opened_read_only() -> None:
    """The matcher's DB handle must reject writes at execute time."""
    from apps.shared.state import db as state_db_mod

    conn = state_db_mod.open_ro(STATE_DB)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE tracks SET file_path = 'x' WHERE 1 = 0")
    finally:
        conn.close()


@pytest.mark.skipif(not HAS_REAL_LIBRARY, reason=_SKIP_REASON)
@pytest.mark.requirement("RECON-01")
def test_real_rows_load_with_streaming_and_absolute_paths() -> None:
    rows = match.load_track_rows(STATE_DB, rb_db=RB_DB)
    assert rows, "if state.db has zero track rows then the projection is empty"
    with sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True) as conn:
        (expected,) = conn.execute("SELECT count(*) FROM tracks").fetchone()
    assert len(rows) == expected, (
        "if loaded rows != count(*) then some rows are silently dropped and "
        "the bucket totals cannot be trusted"
    )


@pytest.mark.slow
@pytest.mark.skipif(not HAS_REAL_LIBRARY, reason=_SKIP_REASON)
@pytest.mark.requirement("RECON-01")
def test_real_library_bucket_counts_sum_to_row_total() -> None:
    """The headline invariant, on the configured real library.

    Uses whatever the on-disk index cache holds (no rebuild) so the test is
    seconds, not minutes. An empty/cold cache is still valid: buckets shift
    toward absent-no-audio but must still sum.
    """
    rows = match.load_track_rows(STATE_DB, rb_db=RB_DB)
    index, _ = index_disk.build_index(
        index_disk.DEFAULT_ROOTS, cache_path=INDEX_CACHE, write_cache=False
    )
    results = match.classify_rows(rows, index)
    counts = match.bucket_counts(results)
    assert sum(counts.values()) == len(rows)
    assert counts["present"] > 0, (
        "if zero rows are present on a machine holding the real library then "
        "existence checking is broken"
    )
    for res in results:
        if res.bucket == "relinkable-auto":
            assert res.best is not None and res.best.auto_applicable
        if res.bucket == "awaiting-volume":
            assert not res.candidates
