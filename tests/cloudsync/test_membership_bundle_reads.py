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

Controls: the probe must count one read per member on the per-member read
path, which production still takes for any parent a walk has not decided;
batched verdicts must equal each member's per-member read on randomized libraries
with stamp faults, deletions, identity losers and identity holds, on both
sides of the real library-election cutover; a replace must still skip, and
warn about, a member whose track is not here.

Every path runs unmodified: counts come from the statements SQLite traces,
and both cutover cases are real bundle sizes. That the growth guards bite is
proved by hand mutation (recorded in the commit that introduced this form),
not by swapping production code inside the suite.
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
from apps.sync_hub.protocol_common import MEMBERSHIP_TABLE
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
COMPONENT_SEED_READ = re.compile(r"FROM tracks WHERE deleted_at IS NULL AND stable_id = ")
"""One per-row identity decision: the seed read of the row's component."""
CUTOVER = identity_verdicts.CFG.ELECT_LIBRARY_AFTER_VERDICTS
"""Read, never set: both sides of it are reached with real bundle sizes."""


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


def _library_elections(conn: sqlite3.Connection, statements: list[str]) -> int:
    """Statements that read every live track's identity: one per library election."""
    library_read = sync_set.identity_row_select(conn)
    return sum(statement.strip() == library_read for statement in statements)


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


def test_probe_counts_a_read_per_member_on_the_per_member_path(tmp_path: Path) -> None:
    """Positive control: the per-member read the batch replaced is still how a
    walk judges a parent it has not decided. Asking it member by member, the
    same probe must count one read each, or a zero above proves nothing."""
    conn = _hub_with_bundle(tmp_path, LARGE_BUNDLE)
    try:
        held = sync_set.HeldKeys(conn)
        with _traced(conn) as statements:
            blocked = [
                held.blocking_parent(MEMBERSHIP_TABLE, {"stable_id": f"stored-{i}"})
                for i in range(LARGE_BUNDLE)
            ]
    finally:
        conn.close()
    assert blocked == [None] * LARGE_BUNDLE
    assert _point_reads(statements, PARENT_POINT_READ) == LARGE_BUNDLE


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


def _per_member_verdicts(
    conn: sqlite3.Connection, pks: list[str], decided: dict[str, bool]
) -> dict[str, bool]:
    """Each track's verdict (True: held) from the per-member read production
    takes for a parent the walk has not decided, one member at a time, on one
    walk that has already decided ``decided``."""
    held = sync_set.HeldKeys(conn)
    for pk, is_held in decided.items():
        (held.hold if is_held else held.release)("tracks", (pk,))
    return {
        pk: held.blocking_parent(MEMBERSHIP_TABLE, {"stable_id": pk}) is not None for pk in pks
    }


#: Bundle sizes, in distinct member tracks, on each side of the cutover.
_BUNDLE_SIZES: dict[str, tuple[int, int]] = {
    "per-row": (40, 90),
    "expect-crosses": (CUTOVER + 40, CUTOVER + 90),
}
#: Library rows per case: enough free tracks that a travelling bundle of the
#: larger size can be drawn from them alone.
_LIBRARY_ROWS: dict[str, int] = {"per-row": 120, "expect-crosses": 2 * CUTOVER}


def _random_bundle(
    conn: sqlite3.Connection, rng: random.Random, pks: list[str], *, size: str, travels: bool
) -> list[str]:
    """Distinct members plus repeats: from every stored track, deleted and
    faulty included, or (``travels``) only from tracks a per-member read finds
    free, so the bundle is sent and every verdict is compared."""
    alone = _per_member_verdicts(conn, pks, {}) if travels else {}
    pool = [pk for pk in pks if not alone.get(pk, False)]
    distinct = rng.sample(pool, min(len(pool), rng.randint(*_BUNDLE_SIZES[size])))
    members = [*distinct, *(rng.choice(distinct) for _ in range(20))]
    rng.shuffle(members)
    _add_playlist(conn, members)
    return distinct


def _walk(
    conn: sqlite3.Connection, walk: str, decided: dict[str, bool]
) -> tuple[Any, frozenset[str], frozenset[str]]:
    """Walk the bundle after the walk has already decided ``decided`` tracks
    (True: held); returns the result and every track the walk then holds or frees."""
    held = sync_set.HeldKeys(conn)
    for pk, is_held in decided.items():
        (held.hold if is_held else held.release)("tracks", (pk,))
    result = _BUNDLE_WALKS[walk](conn, held)
    return (
        result,
        frozenset(held.keys.get("tracks", set())),
        frozenset(held._free.get("tracks", set())),
    )


@pytest.mark.parametrize("seed", range(10))
@pytest.mark.parametrize("walk", sorted(_BUNDLE_WALKS))
@pytest.mark.parametrize("size", sorted(_BUNDLE_SIZES))
def test_batched_verdicts_equal_per_member_verdicts(
    tmp_path: Path, seed: int, walk: str, size: str
) -> None:
    """Overshoot control: a batch that freed or held the wrong tracks would pass
    the read count and send, or hold back, the wrong bundle.

    The expected verdicts come from the per-member read path on a walk that
    predecided the same tracks, and the expected result is the walk given
    exactly those verdicts. ``expect-crosses`` bundles are past the real
    cutover, so the batch elects the library up front while the per-member
    walk decides row by row until it reaches the cutover itself."""
    conn = _open_hub(tmp_path)
    try:
        rng = random.Random(seed)
        pks = _random_library(conn, rng, rows=_LIBRARY_ROWS[size])
        conn.execute(
            "UPDATE tracks SET stable_id_tier = 'inferred', content_hash = NULL, isrc = NULL"
            " WHERE rowid % 7 = 0"
        )
        conn.commit()
        members = _random_bundle(conn, rng, pks, size=size, travels=seed % 2 == 0)
        # Freed only: a walk that held a member holds the bundle, and the
        # travelling case below must stay travelling.
        predecided = {pk: False for pk in rng.sample(pks, 20)}
        undecided = [pk for pk in members if pk not in predecided]
        assert (len(undecided) >= CUTOVER) == (size == "expect-crosses"), (
            "the fixture missed its side of the cutover"
        )
        with _traced(conn) as statements:
            result, held, free = _walk(conn, walk, predecided)
        elections = _library_elections(conn, statements)
        expected = _per_member_verdicts(conn, members, predecided)
        expected_result = _walk(conn, walk, expected)[0]
    finally:
        conn.close()
    assert elections == (1 if size == "expect-crosses" else 0), (
        "the fixture missed its identity path"
    )
    assert _BUNDLE_HELD[walk](expected_result) == (seed % 2 == 1), "the fixture missed its case"
    assert result == expected_result
    decided = {**expected, **predecided}
    assert held == {pk for pk, is_held in decided.items() if is_held}, (
        "the batch held a member track a per-member read frees, or missed one it holds"
    )
    assert free == {pk for pk, is_held in decided.items() if not is_held}, (
        "the batch freed a member track a per-member read holds, or left one undecided"
    )


def _identity_work(tmp_path: Path, members: int) -> tuple[int, int]:
    """(per-row component decisions, library elections) for one bundle walk,
    counted from the statements SQLite traces."""
    conn = _hub_with_bundle(tmp_path, members)
    try:
        with _traced(conn) as statements:
            engine_changes._members_for_playlist(conn, PLAYLIST, sync_set.HeldKeys(conn))
        return _point_reads(statements, COMPONENT_SEED_READ), _library_elections(conn, statements)
    finally:
        conn.close()


def test_a_bundle_past_the_cutover_elects_the_library_once(tmp_path: Path) -> None:
    assert _identity_work(tmp_path, CUTOVER) == (0, 1)


def test_a_bundle_under_the_cutover_still_decides_per_row(tmp_path: Path) -> None:
    """Overshoot control: electing the library for every bundle is the per-page
    election round 2 removed."""
    assert _identity_work(tmp_path, SMALL_BUNDLE) == (SMALL_BUNDLE, 0)


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
