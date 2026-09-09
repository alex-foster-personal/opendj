"""B-3: the parents-first sort must not outrun the guard beneath it.

Contract under test: ``apps/sync_hub/engine_common.py::apply_rank`` and its
three call sites (``engine_changes._changelog_rows`` twice,
``engine_apply._apply`` once).

Round 5 put a parents-before-children sort above the ``spec is None`` guard
in ``_changelog_rows``::

    for table_name, row_pk in sorted(
        latest, key=lambda entry: (_APPLY_ORDER[entry[0]], entry[1])
    ):
        spec = SPEC_BY_TABLE.get(table_name)
        if spec is None:
            raise SyncApplyError(...)          # unreachable

``sorted`` evaluates its key for EVERY element before the loop body runs
once, so an out-of-set table name raises a bare ``KeyError`` out of the
lambda. The guard that produces the useful message -- naming the changelog,
the table and the sync set -- is unreachable for exactly the input it was
written for. ``engine_apply._apply`` carries the same shape one module over.

``local_changelog.table_name`` is a bare ``TEXT NOT NULL`` (migrations_v6_v8),
so this input is storable, not hypothetical: a writer for a table outside the
sync set, a hand-repaired row, or a downgrade past a schema that added a
table all produce it. Callers catch ``SyncApplyError`` and answer 409; a
``KeyError`` escapes as an unhandled 500 on the hub's ``/pull``.

Acceptance criteria, one test each:
- if an out-of-set changelog entry raises anything but ``SyncApplyError``,
  the declared contract is a lie and the hub answers 500 -- broken.
- if the message does not name the table and the changelog, the operator
  cannot find the row to delete -- broken.
- if an in-set changelog entry stops being offered, the fix has been paid
  for by breaking the sync -- broken.
- if ``_APPLY_ORDER`` and ``SPEC_BY_TABLE`` ever answer "in the sync set"
  differently, ``apply_rank`` guards one lookup while a later
  ``SPEC_BY_TABLE[...]`` index does the raising -- broken.

[if] an out-of-set table raises anything but SyncApplyError [then] fail, [else stop].
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.shared.state import sync_stamp
from apps.sync_hub import engine, protocol
from apps.sync_hub.engine_common import _APPLY_ORDER, SyncApplyError, apply_rank
from apps.sync_hub.engine_watermark import Watermark
from apps.sync_hub.protocol import SPEC_BY_TABLE

pytestmark = pytest.mark.requirement("CAT-04")

_DEV = "dev-a"
_T1 = "2026-08-30T10:00:00.000000+00:00"

#: A table name no schema in this repo defines. The literal an operator would
#: see in the message.
_ABSENT_TABLE = "not_a_synced_table"


# ----- helpers ---------------------------------------------------------------


def _log(
    conn: sqlite3.Connection, changelog: str, table: str, pk: tuple[str, ...]
) -> None:
    """Append one changelog entry naming ``table``, bypassing the writer.

    ``sync_stamp.stamp_and_log`` cannot write an out-of-set table name in a
    way this test can rely on, and the point is precisely a row no in-repo
    writer would produce, so the INSERT is direct and matches the DDL.
    """
    conn.execute(
        f"INSERT INTO {changelog}("
        "table_name, row_pk, updated_at, origin_device_id, received_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (table, protocol.encode_row_pk(pk), _T1, _DEV, _T1),
    )


def _seed_track(conn: sqlite3.Connection, stable_id: str) -> None:
    """One orderable, fully stamped ``tracks`` row: the control's payload."""
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, created_at, "
        "updated_at, origin_device_id) VALUES (?, 'inferred', ?, ?, ?, ?)",
        (stable_id, "Control", _T1, _T1, _DEV),
    )


def _seed_machine(conn: sqlite3.Connection, machine_id: str) -> None:
    """``track_locations.machine_id`` carries a FK, so the parent must exist."""
    conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, data_root, "
        "first_seen, last_seen) VALUES (?, ?, 'macos', 0, '/tmp', ?, ?)",
        (machine_id, f"name-{machine_id}", _T1, _T1),
    )


def _fenced() -> Watermark:
    """A watermark past ``needs_full_offer``, so ``spoke_push`` reads the log."""
    return Watermark(peer="hub", last_push_seq=0, last_sync_at=_T1)


# ----- the defect ------------------------------------------------------------


def test_an_out_of_set_local_changelog_entry_raises_the_declared_error(
    conn: sqlite3.Connection,
) -> None:
    """The push side. Round 5's sort raised ``KeyError`` from the lambda."""
    _log(conn, sync_stamp.LOCAL_CHANGELOG_TABLE, _ABSENT_TABLE, ("x",))
    with pytest.raises(SyncApplyError) as caught:
        engine.spoke_push(conn, watermark=_fenced())
    message = str(caught.value)
    assert _ABSENT_TABLE in message
    assert sync_stamp.LOCAL_CHANGELOG_TABLE in message


def test_an_out_of_set_hub_changelog_entry_raises_the_declared_error(
    conn: sqlite3.Connection,
) -> None:
    """The pull side, where the difference is a 409 rather than a 500."""
    _log(conn, "hub_changelog", _ABSENT_TABLE, ("x",))
    with pytest.raises(SyncApplyError) as caught:
        engine.hub_changes_since(conn, 0)
    message = str(caught.value)
    assert _ABSENT_TABLE in message
    assert "hub_changelog" in message


def test_an_out_of_set_offered_row_raises_the_declared_error(
    conn: sqlite3.Connection,
) -> None:
    """``engine_apply._apply``: the same sort-above-guard shape one module over."""
    change = protocol.RowChange(
        table=_ABSENT_TABLE,
        pk=("x",),
        values={"updated_at": _T1, "origin_device_id": _DEV},
    )
    with pytest.raises(SyncApplyError) as caught:
        engine.spoke_apply(conn, [change])
    assert _ABSENT_TABLE in str(caught.value)


def test_the_message_names_the_sync_set_it_checked_against(
    conn: sqlite3.Connection,
) -> None:
    """An operator repairing this needs the allowed names, not just the bad one.

    ``protocol.py`` already prints ``sorted(SPEC_BY_TABLE)`` for the wire-side
    twin of this refusal; this keeps the two refusals equally actionable.
    """
    _log(conn, sync_stamp.LOCAL_CHANGELOG_TABLE, _ABSENT_TABLE, ("x",))
    with pytest.raises(SyncApplyError) as caught:
        engine.spoke_push(conn, watermark=_fenced())
    message = str(caught.value)
    for table in SPEC_BY_TABLE:
        assert table in message


# ----- the controls ----------------------------------------------------------


def test_an_in_set_changelog_entry_is_still_offered(
    conn: sqlite3.Connection,
) -> None:
    """The control that could fail: the guard must not refuse a real table.

    Without this, every assertion above would pass for an engine that had
    been reduced to ``raise SyncApplyError`` unconditionally.
    """
    _seed_track(conn, "trk-1")
    _log(conn, sync_stamp.LOCAL_CHANGELOG_TABLE, "tracks", ("trk-1",))
    offer = engine.spoke_push(conn, watermark=_fenced())
    assert [change.table for change in offer.rows] == ["tracks"]
    assert offer.rows[0].pk == ("trk-1",)


def test_an_in_set_hub_changelog_entry_is_still_pulled(
    conn: sqlite3.Connection,
) -> None:
    """The same control on the pull side."""
    _seed_track(conn, "trk-1")
    _log(conn, "hub_changelog", "tracks", ("trk-1",))
    batch = engine.hub_changes_since(conn, 0)
    assert [change.table for change in batch.rows] == ["tracks"]


def test_a_mixed_batch_still_arrives_parents_before_children(
    conn: sqlite3.Connection,
) -> None:
    """The other control: the sort must still SORT.

    A fix that dropped the ordering to dodge the ``KeyError`` would satisfy
    every assertion above and silently reintroduce the FK-order failure the
    sort exists to prevent, so assert the order, not merely the membership.
    """
    _seed_track(conn, "trk-1")
    _seed_machine(conn, "machine-a")
    conn.execute(
        "INSERT INTO track_locations(location_id, stable_id, machine_id, kind, "
        "role, file_path, created_at, updated_at, origin_device_id) "
        "VALUES (?, ?, 'machine-a', 'local', 'primary', ?, ?, ?, ?)",
        ("loc-1", "trk-1", "/tmp/one.flac", _T1, _T1, _DEV),
    )
    # Child logged FIRST, so a stable sort that did nothing would keep it first.
    _log(conn, sync_stamp.LOCAL_CHANGELOG_TABLE, "track_locations", ("loc-1",))
    _log(conn, sync_stamp.LOCAL_CHANGELOG_TABLE, "tracks", ("trk-1",))
    offer = engine.spoke_push(conn, watermark=_fenced())
    tables = [change.table for change in offer.rows]
    assert tables.index("tracks") < tables.index("track_locations")


# ----- the invariant the guard now rests on ----------------------------------


def test_the_rank_index_and_the_spec_index_agree_on_the_sync_set() -> None:
    """``apply_rank`` guards ONE lookup; the loop bodies then index the other.

    Both dicts are comprehensions over ``protocol.SYNC_TABLES``, so they
    cannot disagree -- but that is the property the removed ``spec is None``
    guards were standing in for, so it is pinned here rather than assumed.
    """
    assert set(_APPLY_ORDER) == set(SPEC_BY_TABLE)
    assert set(_APPLY_ORDER) == {spec.name for spec in protocol.SYNC_TABLES}


def test_apply_rank_orders_parents_below_children() -> None:
    """A direct read of the helper, so a call-site failure is not the only signal."""
    assert apply_rank("tracks", source="unit") < apply_rank(
        "track_locations", source="unit"
    )
    assert apply_rank("tracks", source="unit") < apply_rank(
        "playlists", source="unit"
    )


def test_apply_rank_names_its_caller_in_the_refusal() -> None:
    """``source`` is what tells an operator WHICH log to go and look at."""
    with pytest.raises(SyncApplyError) as caught:
        apply_rank(_ABSENT_TABLE, source="a_named_source")
    assert "a_named_source" in str(caught.value)
    assert _ABSENT_TABLE in str(caught.value)
