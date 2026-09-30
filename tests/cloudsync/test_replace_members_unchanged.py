"""Replacing a playlist bundle with the bundle it already holds must write nothing (LIBM-120 L6).

A first sync of the 10,000-track fixture replaced its 10,042-member playlist
three times: once on the hub, and twice on the spoke -- when the pull brought
the spoke's own playlist back, and when identity repair re-sent it. The two
spoke replaces deleted 10,042 rows and inserted the same 10,042 again, about
0.5 s each, for a bundle that had not changed.

The instrument is the statements SQLite traces for ``playlist_memberships``
writes (``DELETE``/``INSERT``), when a bundle of 10 and of 200 members is
replaced by itself: none, at either size.

[if] replacing a bundle with itself writes rows [then] per-member rewrites, [else stop].

Controls, all through the unmodified production path:
* the probe must count a DELETE and one INSERT per kept member when a real
  replace runs (a bundle a member shorter than the stored one);
* a bundle that differs in one member, one stamp, one value's type, one
  position, or by a member fewer or more, is still replaced;
* on randomized stored and incoming bundles the table ends exactly as the
  unconditional delete-and-insert leaves it;
* a member naming a track this machine has never heard of is still skipped
  with its warning, even when the rest of the bundle is unchanged.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from apps.sync_hub import engine_apply
from tests.cloudsync.test_membership_bundle_reads import PLAYLIST, _hub_with_bundle

pytestmark = pytest.mark.requirement("LIBM-120")

SMALL_BUNDLE = 10
LARGE_BUNDLE = 200
RANDOM_CASES = 40
MEMBERSHIP_WRITE = ("DELETE FROM playlist_memberships", "INSERT INTO playlist_memberships")


# ----- fixtures -----------------------------------------------------------------


def _stored_bundle(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = engine_apply.protocol.table_columns(conn, engine_apply.MEMBERSHIP_TABLE)
    return [
        dict(zip(columns, row, strict=True))
        for row in conn.execute(
            f"SELECT {', '.join(columns)} FROM playlist_memberships "
            "WHERE playlist_id = ? ORDER BY position",
            (PLAYLIST,),
        )
    ]


@contextmanager
def _membership_writes(conn: sqlite3.Connection) -> Iterator[list[str]]:
    writes: list[str] = []

    def trace(statement: str) -> None:
        if statement.startswith(MEMBERSHIP_WRITE):
            writes.append(statement)

    conn.set_trace_callback(trace)
    try:
        yield writes
    finally:
        conn.set_trace_callback(None)


def _writes_replacing_with_itself(tmp_path: Path, members: int) -> int:
    conn = _hub_with_bundle(tmp_path, members)
    try:
        bundle = _stored_bundle(conn)
        assert len(bundle) == members
        with _membership_writes(conn) as writes:
            engine_apply._replace_members(conn, PLAYLIST, bundle)
        assert _stored_bundle(conn) == bundle
        return len(writes)
    finally:
        conn.close()


def _replace_unconditionally(conn: sqlite3.Connection, members: list[dict[str, Any]]) -> None:
    """The reference: delete the bundle, insert every member whose track is stored."""
    conn.execute("DELETE FROM playlist_memberships WHERE playlist_id = ?", (PLAYLIST,))
    stored_tracks = {row[0] for row in conn.execute("SELECT stable_id FROM tracks")}
    for member in members:
        if member["stable_id"] in stored_tracks:
            columns = list(member)
            conn.execute(
                f"INSERT INTO playlist_memberships ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(member.values()),
            )


# ----- the finding --------------------------------------------------------------


@pytest.mark.parametrize("members", [SMALL_BUNDLE, LARGE_BUNDLE])
def test_replacing_a_bundle_with_itself_writes_nothing(tmp_path: Path, members: int) -> None:
    print("if replacing a playlist bundle with an identical one rewrites its rows, then broken")
    writes = _writes_replacing_with_itself(tmp_path, members)
    assert writes == 0, f"{writes} membership writes to replace {members} members with themselves"


def test_probe_counts_every_row_of_a_real_replace(tmp_path: Path) -> None:
    """Probe control: a real replace is seen as one DELETE and one INSERT per kept member."""
    print("if the write probe cannot see a real replace's per-member rows, then broken")
    conn = _hub_with_bundle(tmp_path, SMALL_BUNDLE)
    try:
        shorter = _stored_bundle(conn)[:-1]
        with _membership_writes(conn) as writes:
            engine_apply._replace_members(conn, PLAYLIST, shorter)
        deletes = [w for w in writes if w.startswith(MEMBERSHIP_WRITE[0])]
        inserts = [w for w in writes if w.startswith(MEMBERSHIP_WRITE[1])]
        assert (len(deletes), len(inserts)) == (1, len(shorter)), writes
        assert _stored_bundle(conn) == shorter
    finally:
        conn.close()


# ----- overshoot controls -------------------------------------------------------


def _one_changed(bundle: list[dict[str, Any]], how: str) -> list[dict[str, Any]]:
    changed = [dict(member) for member in bundle]
    if how == "stamp":
        changed[3]["updated_at"] = "2030-01-01T00:00:00.000000+00:00"
    elif how == "origin":
        changed[3]["origin_device_id"] = "another-device"
    elif how == "position":
        changed[3]["position"], changed[4]["position"] = (
            changed[4]["position"],
            changed[3]["position"],
        )
    elif how == "value type":
        changed[3]["position"] = float(changed[3]["position"])
    elif how == "one fewer":
        changed.pop()
    elif how == "tombstone":
        changed[3]["deleted_at"] = "2030-01-01T00:00:00.000000+00:00"
    elif how == "track":
        changed[3]["stable_id"] = changed[0]["stable_id"]
    else:
        raise AssertionError(f"unhandled change {how}")
    return changed


@pytest.mark.parametrize(
    "how", ["stamp", "origin", "position", "value type", "one fewer", "tombstone", "track"]
)
def test_a_bundle_that_differs_in_one_member_is_replaced(tmp_path: Path, how: str) -> None:
    print("if a bundle that differs in one member is skipped as unchanged, then broken")
    conn = _hub_with_bundle(tmp_path, SMALL_BUNDLE)
    try:
        incoming = _one_changed(_stored_bundle(conn), how)
        with _membership_writes(conn) as writes:
            engine_apply._replace_members(conn, PLAYLIST, incoming)
        assert writes, f"a bundle with a changed {how} was skipped"
        assert _stored_bundle(conn) == sorted(incoming, key=lambda member: member["position"])
    finally:
        conn.close()


def test_a_stored_member_the_incoming_bundle_lacks_is_removed(tmp_path: Path) -> None:
    conn = _hub_with_bundle(tmp_path, SMALL_BUNDLE)
    try:
        incoming = _stored_bundle(conn)
        extra = {**incoming[0], "position": incoming[0]["position"] + 100}
        engine_apply._replace_members(conn, PLAYLIST, [*incoming, extra])
        assert len(_stored_bundle(conn)) == len(incoming) + 1
        engine_apply._replace_members(conn, PLAYLIST, incoming)
        assert _stored_bundle(conn) == incoming
    finally:
        conn.close()


def _random_case(
    rng: random.Random, stored: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A stored bundle and an incoming one, each possibly edited from the fixture's."""
    edits: list[Callable[[list[dict[str, Any]]], list[dict[str, Any]]]] = [
        lambda b: b,
        lambda b: _one_changed(b, rng.choice(["stamp", "origin", "position", "tombstone"])),
        lambda b: b[: rng.randint(0, len(b))],
        lambda b: [*b, {**b[0], "position": 999, "stable_id": "never-heard-of"}],
    ]
    return rng.choice(edits)(stored), rng.choice(edits)(stored)


@pytest.mark.parametrize("seed", range(RANDOM_CASES))
def test_the_table_ends_as_the_unconditional_replace_leaves_it(tmp_path: Path, seed: int) -> None:
    print("if skipping an unchanged bundle leaves other rows than a full replace, then broken")
    rng = random.Random(seed)
    ours = _hub_with_bundle(tmp_path / "ours", SMALL_BUNDLE)
    reference = _hub_with_bundle(tmp_path / "reference", SMALL_BUNDLE)
    try:
        fixture = _stored_bundle(ours)
        stored, incoming = _random_case(rng, fixture)
        engine_apply._replace_members(ours, PLAYLIST, stored)
        _replace_unconditionally(reference, stored)
        assert _stored_bundle(ours) == _stored_bundle(reference)
        engine_apply._replace_members(ours, PLAYLIST, incoming)
        _replace_unconditionally(reference, incoming)
        assert _stored_bundle(ours) == _stored_bundle(reference)
    finally:
        ours.close()
        reference.close()


def test_an_unknown_track_is_still_skipped_and_warned_about(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    print("if an unchanged bundle hides the warning about an unknown track, then broken")
    conn = _hub_with_bundle(tmp_path, SMALL_BUNDLE)
    try:
        stored = _stored_bundle(conn)
        incoming = [*stored, {**stored[0], "position": 999, "stable_id": "never-heard-of"}]
        with (
            caplog.at_level(logging.WARNING, logger=engine_apply.log.name),
            _membership_writes(conn) as writes,
        ):
            engine_apply._replace_members(conn, PLAYLIST, incoming)
        assert writes == [], "the stored members equal every member that can be kept"
        assert _stored_bundle(conn) == stored
        assert "never-heard-of is not here yet" in caplog.text
    finally:
        conn.close()


def test_a_schema_mismatch_still_raises_when_the_known_columns_are_unchanged(
    tmp_path: Path,
) -> None:
    print("if an unchanged-looking bundle skips the schema check, then broken")
    conn = _hub_with_bundle(tmp_path, SMALL_BUNDLE)
    try:
        incoming = [{**member, "unexpected_column": 1} for member in _stored_bundle(conn)]
        with pytest.raises(engine_apply.SyncSchemaMismatch, match="unexpected_column"):
            engine_apply._replace_members(conn, PLAYLIST, incoming)
    finally:
        conn.close()
