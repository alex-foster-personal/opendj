"""CLOUDSYNC-07 identity lookup cost must not grow with the library (issue #4397).

Every incoming ``tracks`` row asks the stored library "who shares my
content_hash, audio_hash or ISRC?". When that question is a table scan, a
first sync of n rows costs O(n^2): 10,000 tracks took 76.9 s on agentbox.

The instrument is SQLite's own VM step counter (``set_progress_handler`` at
granularity 1), not wall time, so the verdict does not move with host load.
It counts the whole per-row apply (``hub_apply`` and ``spoke_apply``), so a
second per-row scan added anywhere on that path trips it too, not only the
two lookups this issue names.

[if] per-row apply work at 20x the library is more than 3x the small library's [then] the lookup scans the table, broken.
[if] the probe does not see growth once the identity indexes are dropped [then] the instrument cannot detect a scan, broken.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from apps.sync_hub import engine, protocol
from tests.cloudsync.test_hub_sync import _DEV_A, _DEV_B, _T0, _T1
from tests.cloudsync.test_track_identity_collapse import _open_hub, _values

pytestmark = pytest.mark.requirement("CLOUDSYNC-07")

SMALL_LIBRARY = 100
LARGE_LIBRARY = 2_000
INCOMING_ROWS = 5
MAX_GROWTH = 3.0
"""An indexed lookup is a B-tree seek: its step count barely moves at 20x the
rows. A scan grows with the table, about 20x here. 3x separates the two
with a wide margin either side."""

IDENTITY_INDEXES = ("idx_tracks_content_hash", "idx_tracks_isrc_upper")

Apply = Callable[[sqlite3.Connection, Sequence[protocol.RowChange]], object]


def _digest(label: str, i: int) -> str:
    return hashlib.sha256(f"{label}-{i}".encode()).hexdigest()


def _isrc(label: str, i: int) -> str:
    return f"GB{label}{i:07d}"


def _seed_library(conn: sqlite3.Connection, count: int) -> None:
    """``count`` identity-bearing tracks, each with its own hashes and ISRC."""
    conn.executemany(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, isrc, content_hash,"
        " audio_hash, created_at, updated_at, origin_device_id)"
        " VALUES (?, 'fingerprint', ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                f"stored-{i}", f"stored {i}", _isrc("SEE", i), _digest("c", i),
                _digest("a", i), _T0, _T0, _DEV_A,
            )
            for i in range(count)
        ],
    )
    conn.commit()


def _incoming(conn: sqlite3.Connection) -> list[protocol.RowChange]:
    """New tracks sharing no identity with the library: the worst case, since
    both the hash lookup and the ISRC fallback run to completion."""
    return [
        protocol.RowChange(
            table="tracks",
            pk=(f"incoming-{i}",),
            values=_values(
                conn,
                "tracks",
                stable_id=f"incoming-{i}",
                stable_id_tier="fingerprint",
                title=f"incoming {i}",
                isrc=_isrc("INC", i),
                content_hash=_digest("new-c", i),
                audio_hash=_digest("new-a", i),
                created_at=_T0,
                updated_at=_T1,
                origin_device_id=_DEV_B,
                deleted_at=None,
            ),
        )
        for i in range(INCOMING_ROWS)
    ]


def _apply_steps(conn: sqlite3.Connection, apply: Apply) -> int:
    """SQLite VM steps spent applying the incoming rows, one row per call."""
    steps = 0

    def _count() -> int:
        nonlocal steps
        steps += 1
        return 0

    changes = _incoming(conn)
    conn.set_progress_handler(_count, 1)
    try:
        for change in changes:
            apply(conn, [change])
    finally:
        conn.set_progress_handler(None, 1)
    return steps


def _steps_at(
    tmp_path: Path, count: int, apply: Apply, *, drop_indexes: bool = False
) -> int:
    conn = _open_hub(tmp_path / f"lib-{count}-{drop_indexes}")
    try:
        _seed_library(conn, count)
        if drop_indexes:
            for index in IDENTITY_INDEXES:
                conn.execute(f"DROP INDEX IF EXISTS {index}")
        return _apply_steps(conn, apply)
    finally:
        conn.close()


APPLIES: dict[str, Apply] = {
    "hub_apply": engine.hub_apply,
    "spoke_apply": engine.spoke_apply,
}


@pytest.mark.parametrize("apply_name", sorted(APPLIES))
def test_per_row_apply_work_does_not_grow_with_the_library(
    tmp_path: Path, apply_name: str
) -> None:
    apply = APPLIES[apply_name]
    small = _steps_at(tmp_path, SMALL_LIBRARY, apply)
    large = _steps_at(tmp_path, LARGE_LIBRARY, apply)
    growth = large / small
    assert growth <= MAX_GROWTH, (
        f"{apply_name}: {INCOMING_ROWS} rows cost {small} VM steps against "
        f"{SMALL_LIBRARY} stored tracks and {large} against {LARGE_LIBRARY} "
        f"({growth:.1f}x for {LARGE_LIBRARY // SMALL_LIBRARY}x the library). "
        "Something on the per-row apply path scans the tracks table (#4397)."
    )


def test_probe_sees_the_scan_when_the_identity_indexes_are_gone(
    tmp_path: Path,
) -> None:
    """Negative control: without the indexes this same probe must go red, or
    a green result above says nothing about whether the lookup is indexed."""
    small = _steps_at(tmp_path, SMALL_LIBRARY, engine.hub_apply, drop_indexes=True)
    large = _steps_at(tmp_path, LARGE_LIBRARY, engine.hub_apply, drop_indexes=True)
    assert large / small > MAX_GROWTH * 2, (
        f"unindexed lookup grew only {large / small:.1f}x "
        f"({small} -> {large} steps); the probe cannot tell a scan from a seek"
    )
