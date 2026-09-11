"""``python -m apps.lyrics index --force-rebuild``: the reachable recovery
path for the bulk-removal guard (issue #1343 round-3 review).

The guard in ``apps.lyrics.search_index._remove_stale_rows`` refuses to drop
more than ``MAX_REMOVAL_FRACTION`` of the indexed rows in one batch unless
``force_rebuild=True`` - but before this fix nothing reachable ever passed
that flag, so a legitimately shrunk cache froze the index forever. This
module drives the CLI exactly as an operator would: same subcommand, same
process boundary as the daemon never crosses on its own.

Regression lines:
  - if the CLI has no way to pass force_rebuild through then broken
  - if --force-rebuild does not actually recover a frozen index then broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.lyrics import __main__ as lyrics_cli
from apps.lyrics.cache import cache_path
from apps.lyrics.search_index import (
    LyricsCacheUnavailable,
    count_documents,
    index_batch,
    index_path,
    indexed_ids,
    open_write,
)

from .test_search_index import _write


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _shrink_past_the_bound(data_dir: Path) -> None:
    """Index 4 real lyric entries, then delete 3 of the 4 cache files - a
    75% removal, over MAX_REMOVAL_FRACTION (50%)."""
    for stable_id in ("a", "b", "c", "d"):
        _write(data_dir, stable_id, f"{stable_id} has lyrics about the moonlight")
    for _ in range(1000):
        batch = index_batch(data_dir, max_docs=100)
        if batch.done:
            break
    else:
        raise AssertionError("initial index did not drain")
    for stable_id in ("a", "b", "c"):
        cache_path(data_dir, stable_id).unlink()


def test_frozen_without_force_rebuild_flag(data_dir: Path) -> None:
    """Without --force-rebuild the CLI surfaces the same guard the daemon
    would hit - the index stays frozen, not silently emptied or ignored."""
    _shrink_past_the_bound(data_dir)

    with pytest.raises(LyricsCacheUnavailable, match="per-batch"):
        lyrics_cli.main(["index", "--data-dir", str(data_dir), "--once"])

    conn = open_write(index_path(data_dir))
    try:
        assert indexed_ids(conn) == {"a", "b", "c", "d"}
    finally:
        conn.close()


def test_recovers_with_force_rebuild_flag(data_dir: Path) -> None:
    """--force-rebuild is the reachable recovery: the same shrink that froze
    the index above reconciles cleanly once an operator passes it."""
    _shrink_past_the_bound(data_dir)

    exit_code = lyrics_cli.main(
        ["index", "--data-dir", str(data_dir), "--once", "--force-rebuild"]
    )
    assert exit_code == 0

    conn = open_write(index_path(data_dir))
    try:
        assert indexed_ids(conn) == {"d"}
        assert count_documents(conn) == 1
    finally:
        conn.close()
