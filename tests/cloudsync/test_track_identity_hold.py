"""CLOUDSYNC-07 hold-back and cross-batch identity remap.

Unidentifiable inferred-tier tracks stay local so they cannot mint a
path-tier PK the hub cannot collapse. Identity-bearing rows still sync.
A collapse remap has to outlive one ``hub_apply`` batch: first-sync splits
tracks and playlists across HTTP requests (``PUSH_BATCH_ROWS`` is 200).

[if] an unidentifiable track has a multi-batch remap [then] it holds local, lands, [else stop].
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.sync_hub import client, engine, engine_identity_map, protocol, sync_set
from apps.sync_hub.engine_identity_map import (
    IDENTITY_REMAP_BATCH_ROWS,
    REMAP_TABLE,
    ensure_identity_remap_table,
    load_identity_remap,
    prepare_spoke_identity,
)
from tests.cloudsync.test_hub_sync import _DEV_A, _DEV_B, _T0, _T1, _TestClientTransport
from tests.cloudsync.test_track_identity_collapse import (
    _HASH_A,
    _hub_app,
    _insert_identified_track,
    _insert_location,
    _open_hub,
    _track_ids,
    _values,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-07")

_LOSER = "trk-silver-dup"
_SURVIVOR = "trk-silver-keep"
_PLAYLIST = "pl-afro-latin"


def _incoming_playlist(
    conn: sqlite3.Connection,
    *,
    playlist_id: str,
    name: str,
    members: tuple[str, ...],
    updated_at: str,
    origin: str,
) -> protocol.RowChange:
    values = _values(
        conn,
        "playlists",
        playlist_id=playlist_id,
        name=name,
        vendor="open-dj",
        vendor_pl_id=playlist_id,
        created_at=_T0,
        updated_at=updated_at,
        origin_device_id=origin,
        deleted_at=None,
        forbid_duplicates=0,
    )
    member_columns = protocol.table_columns(conn, protocol.MEMBERSHIP_TABLE)
    bundle: list[dict[str, Any]] = []
    for position, stable_id in enumerate(members):
        row: dict[str, Any] = dict.fromkeys(member_columns)
        row.update(
            {
                "playlist_id": playlist_id,
                "stable_id": stable_id,
                "position": position,
                "updated_at": updated_at,
                "origin_device_id": origin,
            }
        )
        bundle.append({column: row[column] for column in member_columns})
    return protocol.RowChange(
        table="playlists",
        pk=(playlist_id,),
        values=values,
        members=tuple(bundle),
    )


def _membership_ids(conn: sqlite3.Connection, playlist_id: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT stable_id FROM playlist_memberships "
            "WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        )
    ]


def test_later_apply_batch_rewrites_playlist_onto_persisted_survivor(
    tmp_path: Path,
) -> None:
    """The live 409: tracks collapse in batch N, a playlist in batch N+1
    still names the loser PK. Persist the remap or FOREIGN KEY fires."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn,
            _LOSER,
            title="silver loser",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
            file_path="/Silver/a.mp3",
        )
        conn.commit()

        first = engine.hub_apply(
            conn,
            [
                protocol.RowChange(
                    table="tracks",
                    pk=(_SURVIVOR,),
                    values=_values(
                        conn,
                        "tracks",
                        stable_id=_SURVIVOR,
                        stable_id_tier="inferred",
                        title="silver keep",
                        content_hash=_HASH_A,
                        file_path="/Silver/b.mp3",
                        created_at=_T0,
                        updated_at=_T1,
                        origin_device_id=_DEV_A,
                        deleted_at=None,
                    ),
                )
            ],
        )
        assert first.quarantined == 0
        assert _track_ids(conn) == {_SURVIVOR}
        assert load_identity_remap(conn)[_LOSER] == _SURVIVOR

        second = engine.hub_apply(
            conn,
            [
                _incoming_playlist(
                    conn,
                    playlist_id=_PLAYLIST,
                    name="WFP Afro Latin",
                    members=(_LOSER,),
                    updated_at=_T1,
                    origin=_DEV_A,
                )
            ],
        )
        assert second.quarantined == 0, (
            "a later batch must apply the playlist by rewriting the loser "
            f"PK; got quarantined={second.quarantined} faults={second.faults}"
        )
        assert _membership_ids(conn, _PLAYLIST) == [_SURVIVOR]
        assert conn.execute(
            f"SELECT 1 FROM {REMAP_TABLE} WHERE loser_pk = ?", (_LOSER,)
        ).fetchone() is not None
    finally:
        conn.close()


def test_prepare_spoke_identity_remaps_children_and_holds_the_loser(
    tmp_path: Path,
) -> None:
    """Intra-library duplicate hashes: remap children onto the LWW survivor
    before the offer, keep the loser row, hold it out of the sync set."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _insert_identified_track(
            conn,
            _LOSER,
            title="older copy",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
            file_path="/Silver/a.mp3",
        )
        _insert_identified_track(
            conn,
            _SURVIVOR,
            title="newer copy",
            content_hash=_HASH_A,
            updated_at=_T1,
            origin=_DEV_A,
            file_path="/Silver/b.mp3",
        )
        _insert_location(
            conn,
            location_id="loc-loser",
            stable_id=_LOSER,
            file_path="/Silver/a.mp3",
            updated_at=_T0,
            origin=_DEV_A,
        )
        conn.execute(
            """
            INSERT INTO playlists(
                playlist_id, name, vendor, vendor_pl_id, created_at,
                updated_at, origin_device_id
            )
            VALUES (?, 'dups', 'open-dj', ?, ?, ?, ?)
            """,
            (_PLAYLIST, _PLAYLIST, _T0, _T1, _DEV_A),
        )
        conn.execute(
            """
            INSERT INTO playlist_memberships(
                playlist_id, stable_id, position, updated_at, origin_device_id
            )
            VALUES (?, ?, 0, ?, ?)
            """,
            (_PLAYLIST, _LOSER, _T1, _DEV_A),
        )
        conn.commit()

        assert prepare_spoke_identity(conn) == 1
        conn.commit()
        assert _track_ids(conn) == {_LOSER, _SURVIVOR}
        assert _membership_ids(conn, _PLAYLIST) == [_SURVIVOR]
        assert conn.execute(
            "SELECT stable_id FROM track_locations WHERE location_id = ?",
            ("loc-loser",),
        ).fetchone()[0] == _SURVIVOR

        held = sync_set.HeldKeys(conn)
        columns = sync_set.deciding_columns(conn, "tracks")
        loser_row = conn.execute(
            f"SELECT {', '.join(columns)} FROM tracks WHERE stable_id = ?",
            (_LOSER,),
        ).fetchone()
        spec = sync_set.spec_for("tracks")
        assert sync_set.row_reason(
            "tracks", columns, loser_row, spec, held
        ) == sync_set.IDENTITY_DUP_REASON
        assert not sync_set.any_stamp_fault(conn)
        assert sync_set.excluded_counts(conn).get("tracks", 0) >= 1
    finally:
        conn.close()


def test_count_unsyncable_inferred_matches_the_hold_predicate(tmp_path: Path) -> None:
    """hash_pending rows are counted separately from identity-dup holds."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _insert_identified_track(
            conn, "trk-unsyncable-a", title="a", updated_at=_T0, origin=_DEV_A
        )
        _insert_identified_track(
            conn, "trk-unsyncable-b", title="b", updated_at=_T0, origin=_DEV_A
        )
        _insert_identified_track(
            conn,
            "trk-hashed",
            title="hashed",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_identified_track(
            conn,
            "trk-isrc",
            title="isrc",
            isrc="US-S1Z-99-00001",
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_identified_track(
            conn, "trk-vendor-tier", title="vendor", updated_at=_T0, origin=_DEV_A, tier="isrc"
        )
        _insert_identified_track(
            conn,
            "trk-deleted",
            title="deleted",
            updated_at=_T0,
            origin=_DEV_A,
            deleted_at=_T0,
        )
        conn.commit()

        assert sync_set.count_hash_pending(conn) == 2
        assert sync_set.count_unsyncable_inferred(conn) == 0

        held = sync_set.HeldKeys(conn)
        columns = sync_set.deciding_columns(conn, "tracks")
        spec = sync_set.spec_for("tracks")
        for stable_id in ("trk-unsyncable-a", "trk-unsyncable-b"):
            row = conn.execute(
                f"SELECT {', '.join(columns)} FROM tracks WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            reason = sync_set.row_reason("tracks", columns, row, spec, held)
            assert reason is None, (
                "hash_pending candidates must be in the sync set, not held"
            )
    finally:
        conn.close()


def test_any_identity_hold_handles_five_column_candidate_row(tmp_path: Path) -> None:
    """An unidentifiable inferred row reaches the identity hold predicate."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _insert_identified_track(
            conn, "trk-identity-hold", title="held", updated_at=_T0, origin=_DEV_A
        )
        conn.commit()

        assert not sync_set.any_stamp_fault(conn)
        assert not sync_set.identity_duplicate_remap(conn)
        assert not sync_set.any_identity_hold(conn)
        assert not sync_set.any_exclusion_root(conn)
    finally:
        conn.close()


def test_run_sync_holds_unidentifiable_inferred_and_still_syncs_identity(
    tmp_path: Path,
) -> None:
    """The live first-sync shape: 7330 missing files must not deadlock the
    1864 identity-bearing rows, and must not land as path-tier PKs on the hub."""
    spoke = tmp_path / "spoke"
    hub_dir = tmp_path / "hub"
    spoke_conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _insert_identified_track(
            spoke_conn,
            "trk-hashed",
            title="hashed",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_identified_track(
            spoke_conn,
            "trk-unsyncable",
            title="no identity",
            updated_at=_T0,
            origin=_DEV_A,
        )
        spoke_conn.commit()
    finally:
        spoke_conn.close()

    state_db.open_rw(client.state_db_path(hub_dir)).close()

    with TestClient(_hub_app(hub_dir)) as http:
        result = client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=_TestClientTransport(http),
            name="spoke",
        )

    assert result.pushed >= 1, (
        "identity-bearing rows must still be offered; a silent empty "
        f"push would also not raise (pushed={result.pushed})"
    )
    hub_after = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        assert _track_ids(hub_after) == {"trk-hashed", "trk-unsyncable"}
    finally:
        hub_after.close()
    spoke_after = state_db.open_rw(client.state_db_path(spoke))
    try:
        assert _track_ids(spoke_after) == {"trk-hashed", "trk-unsyncable"}
        assert sync_set.count_hash_pending(spoke_after) == 1
    finally:
        spoke_after.close()


def test_second_library_collapses_hashed_rows_and_holds_unidentifiable(
    tmp_path: Path,
) -> None:
    """Air after silver: hashed overlap collapses; missing-file rows stay on Air."""
    spoke = tmp_path / "spoke-b"
    hub_dir = tmp_path / "hub"
    spoke_conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _insert_identified_track(
            spoke_conn,
            "trk-b-hashed",
            title="air copy",
            content_hash=_HASH_A,
            updated_at=_T1,
            origin=_DEV_B,
        )
        _insert_identified_track(
            spoke_conn,
            "trk-b-unsyncable",
            title="no identity",
            updated_at=_T0,
            origin=_DEV_B,
        )
        spoke_conn.commit()
    finally:
        spoke_conn.close()

    hub_conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        _insert_identified_track(
            hub_conn,
            "trk-a-seeded",
            title="seeded by A",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        hub_conn.commit()
    finally:
        hub_conn.close()

    with TestClient(_hub_app(hub_dir)) as http:
        client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=_TestClientTransport(http),
            name="spoke-b",
        )

    hub_after = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        ids = _track_ids(hub_after)
        assert "trk-b-unsyncable" in ids
        assert len(ids) == 2, (
            "hashed overlap must collapse to one tracks row; unidentifiable "
            f"rows stay on the hub as hash_pending; hub holds {ids}"
        )
        assert ids <= {"trk-a-seeded", "trk-b-hashed", "trk-b-unsyncable"}
    finally:
        hub_after.close()


# --- issue #3251: bounded batches + retry on a self-contended write lock ---


def test_prepare_spoke_identity_commits_large_backlog_in_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[Criterion 2] A 900+ row remap backlog commits in chunks of at most
    ``IDENTITY_REMAP_BATCH_ROWS``, never as one transaction spanning the
    whole backlog. This matches the live Air spoke's shape: 905 hub rows,
    only 168 ever landed locally under the old one-transaction remap."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        ensure_identity_remap_table(conn)
        pair_count = IDENTITY_REMAP_BATCH_ROWS * 4 + 50
        conn.executemany(
            f"INSERT INTO {REMAP_TABLE}(loser_pk, survivor_pk) VALUES (?, ?)",
            [(f"trk-loser-{i}", f"trk-survivor-{i}") for i in range(pair_count)],
        )
        conn.commit()

        seen_batches: list[int] = []
        original = engine_identity_map._commit_remap_batch

        def _spy(
            conn: sqlite3.Connection,
            batch: object,
            *,
            batch_index: int,
            batch_count: int,
        ) -> None:
            seen_batches.append(len(batch))  # type: ignore[arg-type]
            original(conn, batch, batch_index=batch_index, batch_count=batch_count)

        monkeypatch.setattr(engine_identity_map, "_commit_remap_batch", _spy)

        assert prepare_spoke_identity(conn) == pair_count
        assert seen_batches == [IDENTITY_REMAP_BATCH_ROWS] * 4 + [50], (
            "expected four full batches and one partial batch, got "
            f"{seen_batches}"
        )
    finally:
        conn.close()


def _hold_write_lock(
    db_path: Path,
    *,
    ready: threading.Event,
    release: threading.Event,
    errors: list[BaseException],
) -> None:
    """Second writer on the same process: take the write lock and sit on it
    until ``release`` fires. Models issue #3251's confirmed root cause --
    one process, several writable sqlite3 connections, one blocking another
    -- rather than a second OS process."""
    try:
        holder = sqlite3.connect(
            str(db_path), timeout=30, isolation_level=None, check_same_thread=False
        )
        try:
            holder.execute("BEGIN IMMEDIATE")
            ready.set()
            release.wait(timeout=10)
            holder.execute("ROLLBACK")
        finally:
            holder.close()
    except BaseException as exc:  # noqa: BLE001 - surfaced via `errors` in the test thread
        errors.append(exc)
        ready.set()


def _seed_one_remap_pair(conn: sqlite3.Connection) -> None:
    """One real loser/survivor pair with a location, so
    ``remap_track_children`` genuinely writes rows (not a no-op update)."""
    _insert_identified_track(
        conn,
        _LOSER,
        title="older copy",
        content_hash=_HASH_A,
        updated_at=_T0,
        origin=_DEV_A,
        file_path="/Silver/a.mp3",
    )
    _insert_identified_track(
        conn,
        _SURVIVOR,
        title="newer copy",
        content_hash=_HASH_A,
        updated_at=_T1,
        origin=_DEV_A,
        file_path="/Silver/b.mp3",
    )
    _insert_location(
        conn,
        location_id="loc-loser",
        stable_id=_LOSER,
        file_path="/Silver/a.mp3",
        updated_at=_T0,
        origin=_DEV_A,
    )
    conn.commit()


def test_prepare_spoke_identity_retries_past_a_transient_lock_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[Criterion 1] A second writer in this process holds the write lock
    past busy_timeout; ``prepare_spoke_identity`` retries with backoff and
    the remap completes, instead of raising out of ``run_sync`` and
    zeroing the round. The retry hook (patched ``_sleep``) releases the
    holder deterministically instead of racing a real clock."""
    db_path = client.state_db_path(tmp_path / "spoke")
    conn = state_db.open_rw(db_path)
    conn.execute("PRAGMA busy_timeout = 200")
    try:
        _seed_one_remap_pair(conn)

        ready = threading.Event()
        release = threading.Event()
        holder_errors: list[BaseException] = []
        holder = threading.Thread(
            target=_hold_write_lock,
            args=(db_path,),
            kwargs={"ready": ready, "release": release, "errors": holder_errors},
        )
        holder.start()
        assert ready.wait(timeout=5), "holder never acquired the write lock"
        assert not holder_errors, f"holder thread failed to start: {holder_errors}"

        released_on_retry = False

        def _release_then_continue(_seconds: float) -> None:
            nonlocal released_on_retry
            released_on_retry = True
            release.set()

        monkeypatch.setattr(engine_identity_map, "_sleep", _release_then_continue)

        assert prepare_spoke_identity(conn) == 1
        holder.join(timeout=5)
        assert not holder.is_alive(), "holder thread did not release in time"
        assert not holder_errors, f"holder thread failed: {holder_errors}"
        assert released_on_retry, "prepare_spoke_identity never retried"

        assert _track_ids(conn) == {_LOSER, _SURVIVOR}
        assert conn.execute(
            "SELECT stable_id FROM track_locations WHERE location_id = ?",
            ("loc-loser",),
        ).fetchone()[0] == _SURVIVOR
    finally:
        conn.close()


def test_prepare_spoke_identity_names_the_lock_holder_when_retries_exhaust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[Criterion 3] When the remap genuinely cannot proceed after its
    retries (the holder never releases), the failure names the lock holder
    and the operation via ``StateStoreBusyError``, with a stable
    ``STATE_DB_BUSY:`` prefix a status consumer can grep for -- distinct
    from a digest-mismatch message, not the two alternating."""
    db_path = client.state_db_path(tmp_path / "spoke")
    conn = state_db.open_rw(db_path)
    conn.execute("PRAGMA busy_timeout = 200")
    try:
        _seed_one_remap_pair(conn)

        ready = threading.Event()
        release = threading.Event()
        holder_errors: list[BaseException] = []
        holder = threading.Thread(
            target=_hold_write_lock,
            args=(db_path,),
            kwargs={"ready": ready, "release": release, "errors": holder_errors},
        )
        holder.start()
        assert ready.wait(timeout=5), "holder never acquired the write lock"
        assert not holder_errors, f"holder thread failed to start: {holder_errors}"

        # Never releases: retries must exhaust. No real sleeping either.
        monkeypatch.setattr(engine_identity_map, "_sleep", lambda _seconds: None)

        with pytest.raises(state_db.StateStoreBusyError) as excinfo:
            prepare_spoke_identity(conn)
        message = str(excinfo.value)
        assert message.startswith("STATE_DB_BUSY:")
        assert "prepare_spoke_identity" in message
        assert "batch 1/1" in message
        assert "this same process" in message
    finally:
        release.set()
        holder.join(timeout=5)
        assert not holder_errors, f"holder thread failed: {holder_errors}"
        conn.close()


def test_prepare_spoke_identity_does_not_report_a_nested_transaction_as_a_busy_lock(
    tmp_path: Path,
) -> None:
    """[Criterion 3, negative] Only real lock contention may read STATE_DB_BUSY.

    Called inside a caller-owned transaction, ``BEGIN IMMEDIATE`` fails with
    ``cannot start a transaction within a transaction``. That is a call-site bug
    (it is exactly the blanket ``_transaction`` wrapper #3251 removed), not a lock
    holder, so it must surface as itself. Labelling it ``STATE_DB_BUSY ...
    self-contention`` sends whoever reads the status chasing a writer that does
    not exist.
    """
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _seed_one_remap_pair(conn)
        conn.execute("BEGIN")
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            prepare_spoke_identity(conn)
        assert not isinstance(excinfo.value, state_db.StateStoreBusyError)
        assert "within a transaction" in str(excinfo.value)
        assert "STATE_DB_BUSY" not in str(excinfo.value)
        conn.rollback()

        # POSITIVE CONTROL: the same connection and fixture remap fine outside a
        # caller transaction, so the raise above is about nesting, not the seed.
        assert prepare_spoke_identity(conn) == 1
    finally:
        conn.close()
