"""A playlist bundle must not read its tracks one member at a time (LIBM-120 L6).

A first sync of the 10,000-track fixture materializes its 10,042-member
playlist three times on the hub and replaces it three times, once on the hub
and twice on the spoke. Each time it ran one statement per member for
bookkeeping alone:

* materializing a bundle in a walk that had not visited the member tracks
  read each parent track with its own ``SELECT ... WHERE stable_id = ?``;
* replacing a bundle checked each member's track exists with its own
  ``SELECT 1 FROM tracks WHERE stable_id = ?`` before inserting it.

The instrument is the statements SQLite traces (``set_trace_callback``),
matched on those two point reads, at 10 and at 200 members.

[if] a bundle's point reads grow with its member count [then] per-member reads, [else stop].

Controls: the probe must count one read per member when the batched decision
is taken out; batched verdicts must equal per-member verdicts on randomized
libraries with stamp faults, deletions, identity losers and identity holds;
a replace must still skip, and warn about, a member whose track is not here.
"""
from __future__ import annotations

import logging
import random
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from apps.sync_hub import engine_apply, engine_changes, identity_verdicts, sync_set
from tests.cloudsync.test_hub_sync import _DEV_A, _T0, _T1
from tests.cloudsync.test_identity_verdicts import _random_library
from tests.cloudsync.test_track_identity_collapse import _open_hub
from tests.cloudsync.test_track_identity_lookup_scale import _seed_library

pytestmark = pytest.mark.requirement("LIBM-120")

SMALL_BUNDLE = 10
LARGE_BUNDLE = 200
PLAYLIST = "pl-bundle"
PARENT_POINT_READ = re.compile(r"FROM tracks WHERE stable_id = ")
EXISTS_POINT_READ = re.compile(r"SELECT 1 FROM tracks WHERE stable_id = ")


# ----- fixtures -----------------------------------------------------------------


def _add_playlist(conn: sqlite3.Connection, stable_ids: list[str]) -> None:
    conn.execute(
        "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, created_at,"
        " updated_at, origin_device_id) VALUES (?, 'bundle', 'open-dj', ?, ?, ?, ?)",
        (PLAYLIST, PLAYLIST, _T0, _T1, _DEV_A),
    )
    conn.executemany(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position, updated_at,"
        " origin_device_id) VALUES (?, ?, ?, ?, ?)",
        [
            (PLAYLIST, stable_id, position, _T1, _DEV_A)
            for position, stable_id in enumerate(stable_ids)
        ],
    )
    conn.commit()


def _hub_with_bundle(tmp_path: Path, members: int) -> sqlite3.Connection:
    conn = _open_hub(tmp_path / f"bundle-{members}")
    _seed_library(conn, members)
    _add_playlist(conn, [f"stored-{i}" for i in range(members)])
    return conn


@contextmanager
def _traced(conn: sqlite3.Connection) -> Iterator[list[str]]:
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        yield statements
    finally:
        conn.set_trace_callback(None)


def _point_reads(statements: list[str], pattern: re.Pattern[str]) -> int:
    return sum(bool(pattern.search(statement)) for statement in statements)


def _per_member_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the batched decision out: every parent is read when first asked."""
    monkeypatch.setattr(sync_set.HeldKeys, "decide_member_tracks", lambda self, playlist_id: None)


# ----- growth guards ------------------------------------------------------------

_BUNDLE_WALKS: dict[str, Callable[[sqlite3.Connection, sync_set.HeldKeys], object]] = {
    "materialize": lambda conn, held: engine_changes._members_for_playlist(conn, PLAYLIST, held),
    "membership_reason": lambda conn, held: sync_set.membership_reason(conn, PLAYLIST, held),
}

#: Whether a walk's result says the bundle was held back.
_BUNDLE_HELD: dict[str, Callable[[object], bool]] = {
    "materialize": lambda result: result is None,
    "membership_reason": lambda result: result is not None,
}


def _parent_reads(tmp_path: Path, members: int, walk: str) -> int:
    conn = _hub_with_bundle(tmp_path, members)
    try:
        with _traced(conn) as statements:
            _BUNDLE_WALKS[walk](conn, sync_set.HeldKeys(conn))
        return _point_reads(statements, PARENT_POINT_READ)
    finally:
        conn.close()


@pytest.mark.parametrize("walk", sorted(_BUNDLE_WALKS))
def test_a_bundle_reads_its_tracks_in_one_statement(tmp_path: Path, walk: str) -> None:
    small = _parent_reads(tmp_path, SMALL_BUNDLE, walk)
    large = _parent_reads(tmp_path, LARGE_BUNDLE, walk)
    assert (small, large) == (0, 0), (
        f"{walk}: {small} parent point reads for {SMALL_BUNDLE} members, {large} for "
        f"{LARGE_BUNDLE}. The bundle reads its tracks one member at a time (LIBM-120 L6)."
    )


@pytest.mark.parametrize("walk", sorted(_BUNDLE_WALKS))
def test_probe_counts_a_read_per_member_without_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, walk: str
) -> None:
    """Negative control: with per-member decisions back, the same probe must count them."""
    _per_member_decisions(monkeypatch)
    assert _parent_reads(tmp_path, LARGE_BUNDLE, walk) == LARGE_BUNDLE


def _replace_exists_reads(tmp_path: Path, members: int) -> tuple[int, int]:
    conn = _hub_with_bundle(tmp_path, members)
    try:
        bundle = engine_changes._members_for_playlist(conn, PLAYLIST, sync_set.HeldKeys(conn))
        assert bundle is not None and len(bundle) == members
        conn.execute("BEGIN")
        with _traced(conn) as statements:
            engine_apply._replace_members(conn, PLAYLIST, bundle)
        stored = conn.execute(
            "SELECT count(*) FROM playlist_memberships WHERE playlist_id = ?", (PLAYLIST,)
        ).fetchone()[0]
        conn.execute("ROLLBACK")
        return _point_reads(statements, EXISTS_POINT_READ), stored
    finally:
        conn.close()


def test_a_replace_checks_its_tracks_exist_in_one_statement(tmp_path: Path) -> None:
    small, small_stored = _replace_exists_reads(tmp_path, SMALL_BUNDLE)
    large, large_stored = _replace_exists_reads(tmp_path, LARGE_BUNDLE)
    assert (small_stored, large_stored) == (SMALL_BUNDLE, LARGE_BUNDLE)
    assert (small, large) == (0, 0), (
        f"replace ran {small} track-exists reads for {SMALL_BUNDLE} members and "
        f"{large} for {LARGE_BUNDLE}: one per member (LIBM-120 L6)"
    )


# ----- equivalence --------------------------------------------------------------


def _travels_alone(conn: sqlite3.Connection, pk: str) -> bool:
    return sync_set._stored_row_reason(conn, "tracks", pk, sync_set.HeldKeys(conn)) is None


def _random_bundle(
    conn: sqlite3.Connection, rng: random.Random, pks: list[str], *, travels: bool
) -> None:
    """Members drawn with repeats: from every stored track, deleted and faulty
    included, or (``travels``) only from tracks a lone read finds free, so the
    bundle is sent and every verdict is compared."""
    pool = [pk for pk in pks if not travels or _travels_alone(conn, pk)]
    _add_playlist(conn, [rng.choice(pool) for _ in range(rng.randint(40, 90))])


def _verdicts(conn: sqlite3.Connection, walk: str) -> tuple[Any, ...]:
    held = sync_set.HeldKeys(conn)
    result = _BUNDLE_WALKS[walk](conn, held)
    return (
        result,
        frozenset(held.keys.get("tracks", set())),
        frozenset(held._free.get("tracks", set())),
    )


@pytest.mark.parametrize("seed", range(10))
@pytest.mark.parametrize("walk", sorted(_BUNDLE_WALKS))
@pytest.mark.parametrize("elect_after", [50, 10**9], ids=["expect-crosses", "per-row"])
def test_batched_verdicts_equal_per_member_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int, walk: str, elect_after: int
) -> None:
    """Overshoot control: a batch that freed or held the wrong tracks would pass
    the read count and send, or hold back, the wrong bundle."""
    monkeypatch.setattr(identity_verdicts.CFG, "ELECT_LIBRARY_AFTER_VERDICTS", elect_after)
    conn = _open_hub(tmp_path)
    try:
        rng = random.Random(seed)
        pks = _random_library(conn, rng, rows=120)
        conn.execute(
            "UPDATE tracks SET stable_id_tier = 'inferred', content_hash = NULL, isrc = NULL"
            " WHERE rowid % 7 = 0"
        )
        _random_bundle(conn, rng, pks, travels=seed % 2 == 0)
        batched = _verdicts(conn, walk)
        with monkeypatch.context() as per_member:
            _per_member_decisions(per_member)
            expected = _verdicts(conn, walk)
    finally:
        conn.close()
    (result, held, free), (expected_result, expected_held, expected_free) = batched, expected
    assert result == expected_result
    # A held bundle stops the per-member walk at its first held member; the
    # batch has decided every member by then. Each track either path decided
    # must carry the same verdict in both.
    assert expected_held <= held and expected_free <= free, "a per-member verdict is missing"
    assert not (held & expected_free) and not (free & expected_held), (
        "the batch decided a member track differently from a per-member read"
    )
    assert _BUNDLE_HELD[walk](expected_result) == (seed % 2 == 1), "the fixture missed its case"
    if not _BUNDLE_HELD[walk](expected_result):
        assert (held, free) == (expected_held, expected_free)


def test_replace_still_skips_a_member_whose_track_is_not_here(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    conn = _hub_with_bundle(tmp_path, SMALL_BUNDLE)
    try:
        bundle = engine_changes._members_for_playlist(conn, PLAYLIST, sync_set.HeldKeys(conn))
        assert bundle is not None
        ghost = {**bundle[3], "stable_id": "never-heard-of-it"}
        offered = (*bundle[:3], ghost, *bundle[4:])
        conn.execute("BEGIN")
        with caplog.at_level(logging.WARNING, logger=engine_apply.log.name):
            engine_apply._replace_members(conn, PLAYLIST, offered)
        stored = [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT position, stable_id FROM playlist_memberships WHERE playlist_id = ?"
                " ORDER BY position",
                (PLAYLIST,),
            )
        ]
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    assert stored == [(m["position"], m["stable_id"]) for m in offered if m is not ghost]
    assert "never-heard-of-it is not here yet" in caplog.text
