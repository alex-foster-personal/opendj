"""A hub pull page must not cost more in a bigger library (LIBM-120 L6).

Two per-row costs made a 10,000-track first sync slow:

* every :class:`HeldKeys` (one per hub pull page) elected the WHOLE library
  to learn which tracks lose an identity election: quadratic in the library;
* every row asked ``PRAGMA table_info`` for its table's columns.

The instruments are SQLite's own: VM steps (``set_progress_handler`` at
granularity 1) and the statements it traces, not wall time, so the verdict
does not move with host load.

[if] a hub pull page's work grows with the library [then] per-page identity election, [else stop].

Controls: the probe must see growth when the library-wide election is put
back; per-row verdicts must equal the library-wide election on randomized
libraries with odd stored keys, stamp faults, deletions and persisted remap
chains; the column memo must never answer for another connection, nor
outlive its scope.
"""
from __future__ import annotations

import random
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from apps.sync_hub import engine, identity_verdicts, protocol, sync_set
from apps.sync_hub.engine_identity_map import (
    effective_identity_remap,
    ensure_identity_remap_table,
)
from tests.cloudsync.test_hub_sync import _DEV_A, _DEV_B, _T0, _T1
from tests.cloudsync.test_track_identity_collapse import _open_hub
from tests.cloudsync.test_track_identity_lookup_scale import _incoming, _seed_library

pytestmark = pytest.mark.requirement("CLOUDSYNC-07")

SMALL_LIBRARY = 100
LARGE_LIBRARY = 2_000
MAX_GROWTH = 3.0
"""Per-row verdicts are index seeks: flat at 20x the library. A library-wide
election grows with it, about 20x here."""


# ----- growth guard -------------------------------------------------------------


@contextmanager
def _counting_steps(conn: sqlite3.Connection) -> Iterator[list[int]]:
    steps = [0]

    def _count() -> int:
        steps[0] += 1
        return 0

    conn.set_progress_handler(_count, 1)
    try:
        yield steps
    finally:
        conn.set_progress_handler(None, 1)


def _pull_page_steps(tmp_path: Path, count: int) -> int:
    """VM steps to serve one pull page of new tracks from a hub holding ``count``."""
    conn = _open_hub(tmp_path / f"lib-{count}")
    try:
        _seed_library(conn, count)
        since = engine.current_seq(conn)
        engine.hub_apply(conn, _incoming(conn))
        conn.commit()
        with _counting_steps(conn) as steps:
            batch = engine.hub_changes_since(conn, since)
        assert len(batch.rows) == len(_incoming(conn)), batch
        return steps[0]
    finally:
        conn.close()


def _library_wide_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put back what every HeldKeys did before: elect the whole library."""

    def _elect(self: sync_set.HeldKeys, stable_id: str) -> bool:
        return stable_id in effective_identity_remap(self._conn)

    monkeypatch.setattr(sync_set.HeldKeys, "is_identity_loser", _elect)


def test_a_pull_page_costs_the_same_in_a_bigger_library(tmp_path: Path) -> None:
    small = _pull_page_steps(tmp_path, SMALL_LIBRARY)
    large = _pull_page_steps(tmp_path, LARGE_LIBRARY)
    growth = large / small
    assert growth <= MAX_GROWTH, (
        f"one pull page cost {small} VM steps against {SMALL_LIBRARY} stored "
        f"tracks and {large} against {LARGE_LIBRARY} ({growth:.1f}x). Something "
        "on the hub pull path reads the whole tracks table per page (LIBM-120 L6)."
    )


def test_probe_sees_growth_when_every_page_elects_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: with the old election back, the same probe must go red."""
    _library_wide_verdicts(monkeypatch)
    small = _pull_page_steps(tmp_path, SMALL_LIBRARY)
    large = _pull_page_steps(tmp_path, LARGE_LIBRARY)
    assert large / small > MAX_GROWTH * 2, (
        f"library-wide election grew only {large / small:.1f}x ({small} -> {large} "
        "steps); the probe cannot tell a per-page scan from per-row seeks"
    )


# ----- verdict equivalence ------------------------------------------------------

_KEYS = tuple(f"k{i:02d}" for i in range(24))
_ODD_FORMS = (
    lambda key: f" {key}", lambda key: f"{key} ", lambda key: f"\t{key}",
    lambda key: f"\xa0{key}", lambda key: key.encode(), lambda key: f"{key}x",
)
_ISRCS: tuple[object, ...] = (
    None, "GBAAA2600001", "gb-aaa-26-00001", "GB AAA 26 00001", "USBBB2600002",
    "usbbb2600002", "bad", "",
)
_BLANK_HASHES: tuple[object, ...] = (None, "", "  ")


def _random_hash(rng: random.Random, present: float) -> object:
    """Mostly canonical, sometimes padded, a BLOB, or a longer key sharing the prefix."""
    if rng.random() >= present:
        return rng.choice(_BLANK_HASHES)
    key = rng.choice(_KEYS)
    return rng.choice(_ODD_FORMS)(key) if rng.random() < 0.3 else key


def _random_library(conn: sqlite3.Connection, rng: random.Random, rows: int) -> list[str]:
    """Sparse identity keys, so a row often shares one ONLY in an odd stored form."""
    pks = [f"t{i:03d}" for i in range(rows)]
    conn.executemany(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, isrc, content_hash,"
        " audio_hash, created_at, updated_at, origin_device_id, deleted_at)"
        " VALUES (?, 'fingerprint', ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                pk, pk, rng.choice(_ISRCS), _random_hash(rng, 0.6), _random_hash(rng, 0.3),
                _T0, "not a stamp" if rng.random() < 0.1 else rng.choice((_T0, _T1)),
                rng.choice((_DEV_A, _DEV_B)), _T1 if rng.random() < 0.1 else None,
            )
            for pk in pks
        ],
    )
    ensure_identity_remap_table(conn)
    conn.executemany(
        "INSERT OR REPLACE INTO sync_identity_remap(loser_pk, survivor_pk) VALUES (?, ?)",
        [(rng.choice(pks), rng.choice(pks)) for _ in range(rows // 10)],
    )
    conn.commit()
    return pks


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("elect_after", [0, 10**9], ids=["library", "per-row"])
def test_per_row_verdicts_equal_the_library_wide_election(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int, elect_after: int
) -> None:
    """Overshoot control: a verdict that skipped any row sharing a key (a
    padded hash, a BLOB, a separator-formatted ISRC) would pass the growth
    test and hold or release the wrong track."""
    monkeypatch.setattr(identity_verdicts.CFG, "ELECT_LIBRARY_AFTER_VERDICTS", elect_after)
    conn = _open_hub(tmp_path)
    try:
        pks = _random_library(conn, random.Random(seed), rows=120)
        expected = set(effective_identity_remap(conn))
        assert expected, "the fixture elected no loser, so it tests nothing"
        order = [*pks, "absent"]
        random.Random(seed).shuffle(order)
        held = sync_set.HeldKeys(conn)
        actual = {pk for pk in order if held.is_identity_loser(pk)}
    finally:
        conn.close()
    assert actual == expected


# ----- column memo --------------------------------------------------------------


def _table_info_statements(tmp_path: Path, changes: int) -> int:
    conn = _open_hub(tmp_path / f"apply-{changes}")
    traced: list[str] = []
    try:
        incoming = _incoming(conn)[:changes]
        conn.set_trace_callback(traced.append)
        engine.hub_apply(conn, incoming)
    finally:
        conn.set_trace_callback(None)
        conn.close()
    return sum("table_info" in statement for statement in traced)


def test_an_apply_batch_reads_each_tables_columns_once(tmp_path: Path) -> None:
    few = _table_info_statements(tmp_path, 1)
    many = _table_info_statements(tmp_path, 5)
    assert many == few, (
        f"PRAGMA table_info ran {few} times for 1 row and {many} for 5: the "
        "apply batch re-reads a table's columns per row (LIBM-120 L6)"
    )


def test_the_column_memo_never_answers_for_another_connection(tmp_path: Path) -> None:
    hub = _open_hub(tmp_path)
    other = sqlite3.connect(":memory:")
    try:
        other.execute("CREATE TABLE tracks(stable_id TEXT PRIMARY KEY)")
        with protocol.table_columns_memo(hub):
            assert "content_hash" in protocol.table_columns(hub, "tracks")
            assert protocol.table_columns(other, "tracks") == ("stable_id",)
    finally:
        hub.close()
        other.close()


def test_the_column_memo_ends_with_its_scope() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE tracks(stable_id TEXT PRIMARY KEY)")
        with protocol.table_columns_memo(conn):
            assert protocol.table_columns(conn, "tracks") == ("stable_id",)
        conn.execute("ALTER TABLE tracks ADD COLUMN title TEXT")
        assert protocol.table_columns(conn, "tracks") == ("stable_id", "title")
    finally:
        conn.close()
