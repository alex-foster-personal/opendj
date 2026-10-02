"""CLOUDSYNC-30: a stale or copied library cannot bring back tracks the fleet dropped.

Contract: ``apps/sync_hub/stale_copy.py`` and
``docs/decisions/ADR-NEW-deletes-stay-deleted.md`` ("Stale copies"), issue
#4628. Every scenario runs the real hub router and the real ``run_sync``.

The fixture is the incident: two machines share a library, the fleet then
drops a track with NO tombstone (the 166 tracks of #4628 were dropped that
way, before deletes were recorded), and a copy of a library taken before the
drop syncs against the hub.

- [if] the stale copy runs its first sync [then] it is refused, naming the dropped track, and nothing reaches the hub, [else stop].
- [if] the copy carried its source's watermark [then] the digest mismatch is named as a stale library, [else stop].
- [if] the user runs ``stale-tracks --remove`` [then] the next sync completes and the hub holds the deletion, [else stop].
- [if] the user runs ``stale-tracks --keep`` [then] the track returns to every machine, [else stop].
- [if] a new machine brings only its own tracks [then] its first sync completes, [else stop].
- [if] the hub was restored and forgot rows [then] a spoke re-seeding it is not refused, [else stop].
- [if] the spoke skips its own check [then] the hub's push refuses the batch, [else stop].

The last four are controls: the guard must not refuse new work, a hub
restore, or leave the user without a way forward.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.shared.state import machine_identity
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter, compute_playlist_id
from apps.sync_hub import client, client_stale_copy, generation, stale_copy
from apps.sync_hub.transport import SyncTransportError
from tests.cloudsync.test_hub_sync import (
    _T0,
    _insert_track,
    _open,
    _sync,
    _TestClientTransport,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-30")

_DROPPED = "trk-dropped"
_KEPT = "trk-kept"


@pytest.fixture
def spoke_c(tmp_path: Path) -> Path:
    """A third data dir: the stale copy, or a genuinely new machine."""
    return tmp_path / "spoke-c"


# ----- helpers -----------------------------------------------------------


def _machine(data_dir: Path) -> str:
    return machine_identity.get_or_create_machine_id(data_dir)


def _writer(conn: sqlite3.Connection) -> StateWriter:
    return StateWriter(conn, bus=FakeEventBus(), actor="test")


def _add_track(data_dir: Path, stable_id: str) -> None:
    """A track this machine wrote itself, as an ingest would stamp it."""
    conn = _open(data_dir)
    try:
        _insert_track(conn, stable_id, title=stable_id, updated_at=_T0, origin=_machine(data_dir))
        conn.commit()
    finally:
        conn.close()


def _decorate(data_dir: Path, stable_id: str) -> None:
    """A field row, a vendor id and a playlist membership under ``stable_id``."""
    conn = _open(data_dir)
    try:
        writer = _writer(conn)
        try:
            writer.set_vendor_id(stable_id, "rekordbox", f"rb-{stable_id}")
            writer.set_field(
                stable_id, "bpm", 124.0, source="manual", modified_at="2026-09-01T10:00:00.000000+00:00"
            )
            playlist_id = compute_playlist_id("open-dj", "warmup")
            writer.insert_playlist(playlist_id=playlist_id, name="Warmup", vendor="open-dj", vendor_pl_id="warmup")
            writer.set_playlist_memberships(playlist_id, [_KEPT, stable_id])
        finally:
            writer.close()
    finally:
        conn.close()


def _drop_without_tombstone(data_dir: Path, stable_id: str) -> None:
    """Remove every row of ``stable_id``, as the pre-tombstone fleet did.

    ``hub_changelog`` keeps its entries. Deleting them would lower the hub's
    ``MAX(seq)``, which the hub reads as a restore and rotates its
    generation: that is the reseed control's scenario, not this one. A
    dangling entry is served as ``skipped`` by the pull.
    """
    conn = _open(data_dir)
    try:
        for table in ("playlist_memberships", "track_fields", "track_vendor_ids", "track_locations"):
            conn.execute(f"DELETE FROM {table} WHERE stable_id = ?", (stable_id,))
        conn.execute("DELETE FROM tracks WHERE stable_id = ?", (stable_id,))
        conn.execute("DELETE FROM local_changelog WHERE row_pk LIKE ?", (f"%{stable_id}%",))
        conn.commit()
    finally:
        conn.close()


def _copy_library(source: Path, target: Path, *, keep_watermark: bool) -> None:
    """``target`` gets ``source``'s state DB but NOT its machine-id file.

    That is what a restored backup or a copied library looks like: the rows
    keep their authors, and the machine running them is a new one.
    """
    src = _open(source)
    dst_path = client.state_db_path(target)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    if not keep_watermark:
        conn = _open(target)
        try:
            conn.execute("DELETE FROM sync_state")
            conn.commit()
        finally:
            conn.close()


def _row(data_dir: Path, sql: str, *params: object) -> tuple[Any, ...] | None:
    conn = _open(data_dir)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _track(data_dir: Path, stable_id: str) -> tuple[object, ...] | None:
    """``(deleted_at, origin_device_id)`` of one track, or None when absent."""
    return _row(data_dir, "SELECT deleted_at, origin_device_id FROM tracks WHERE stable_id = ?", stable_id)


def _children(data_dir: Path, stable_id: str) -> int:
    counted = _row(
        data_dir,
        "SELECT (SELECT COUNT(*) FROM track_fields WHERE stable_id = ?)"
        " + (SELECT COUNT(*) FROM track_vendor_ids WHERE stable_id = ?)"
        " + (SELECT COUNT(*) FROM playlist_memberships WHERE stable_id = ?)",
        stable_id,
        stable_id,
        stable_id,
    )
    assert counted is not None
    return int(counted[0])


def _changelog_size(hub_dir: Path) -> int:
    counted = _row(hub_dir, "SELECT COUNT(*) FROM hub_changelog")
    assert counted is not None
    return int(counted[0])


def _fleet_dropped_a_track(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, spoke_c: Path, *, keep_watermark: bool
) -> None:
    """A wrote two tracks, B decorated one, the copy was taken, the fleet dropped it."""
    _add_track(spoke_a, _KEPT)
    _add_track(spoke_a, _DROPPED)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    _decorate(spoke_b, _DROPPED)
    _sync(spoke_b, hub, "spoke-b")
    _copy_library(spoke_b, spoke_c, keep_watermark=keep_watermark)
    for machine in (hub_dir, spoke_a, spoke_b):
        _drop_without_tombstone(machine, _DROPPED)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    assert _track(hub_dir, _DROPPED) is None


def _stale_tracks(spoke: Path, hub: _TestClientTransport, action: str | None) -> int:
    args = argparse.Namespace(data_dir=spoke, hub="http://hub.invalid", action=action)
    return client_stale_copy.run_cli(args, transport=hub)


# ----- the stale copy ------------------------------------------------------


def test_a_stale_copy_first_sync_is_refused_and_pushes_nothing(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, spoke_c: Path
) -> None:
    _fleet_dropped_a_track(hub, hub_dir, spoke_a, spoke_b, spoke_c, keep_watermark=False)
    before = _changelog_size(hub_dir)

    with pytest.raises(client.SyncStaleTracks) as refused:
        _sync(spoke_c, hub, "spoke-c")

    assert refused.value.orphans == (_DROPPED,)
    assert "stale-tracks" in str(refused.value), "the refusal must name its remedy"
    assert _changelog_size(hub_dir) == before, "a refused stale copy still moved rows onto the hub"
    for machine in (hub_dir, spoke_a, spoke_b):
        assert _track(machine, _DROPPED) is None, f"{machine.name}: the dropped track came back"
        assert _children(machine, _DROPPED) == 0, f"{machine.name}: a dropped track's rows came back"
    assert _track(spoke_c, _DROPPED) == (None, _machine(spoke_a)), "the copy's own row was touched"


def test_a_copy_that_kept_its_watermark_is_named_stale_not_corrupt(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, spoke_c: Path
) -> None:
    """Its fenced offer carries none of the dropped rows, so only the digest sees them."""
    _fleet_dropped_a_track(hub, hub_dir, spoke_a, spoke_b, spoke_c, keep_watermark=True)

    with pytest.raises(client.SyncStaleTracks) as refused:
        _sync(spoke_c, hub, "spoke-c")

    assert refused.value.orphans == (_DROPPED,)
    assert isinstance(refused.value.__cause__, client.SyncDigestMismatch)
    assert _track(hub_dir, _DROPPED) is None


def test_remove_resolves_the_copy_and_the_hub_then_holds_the_deletion(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, spoke_c: Path
) -> None:
    _fleet_dropped_a_track(hub, hub_dir, spoke_a, spoke_b, spoke_c, keep_watermark=False)
    with pytest.raises(client.SyncStaleTracks):
        _sync(spoke_c, hub, "spoke-c")

    assert _stale_tracks(spoke_c, hub, None) == client_stale_copy.EXIT_STALE_LISTED
    assert _track(spoke_c, _DROPPED) == (None, _machine(spoke_a)), "listing changed the library"
    assert _stale_tracks(spoke_c, hub, "remove") == 0

    _sync(spoke_c, hub, "spoke-c")
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    hub_row = _track(hub_dir, _DROPPED)
    assert hub_row is not None and hub_row[0] is not None, "the hub does not hold the deletion"
    for machine in (hub_dir, spoke_a, spoke_b, spoke_c):
        state = _track(machine, _DROPPED)
        assert state is None or state[0] is not None, f"{machine.name}: the dropped track is live"
        assert _row(
            machine,
            "SELECT COUNT(*) FROM playlist_memberships WHERE stable_id = ? AND deleted_at IS NULL",
            _DROPPED,
        ) == (0,), f"{machine.name}: the dropped track is back in a playlist"
    assert _stale_tracks(spoke_c, hub, None) == 0


def test_keep_returns_the_track_to_every_machine(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, spoke_c: Path
) -> None:
    """Overshoot control: the guard must leave a way to want the track back."""
    _fleet_dropped_a_track(hub, hub_dir, spoke_a, spoke_b, spoke_c, keep_watermark=False)
    with pytest.raises(client.SyncStaleTracks):
        _sync(spoke_c, hub, "spoke-c")

    assert _stale_tracks(spoke_c, hub, "keep") == 0
    _sync(spoke_c, hub, "spoke-c")
    _sync(spoke_a, hub, "spoke-a")

    for machine in (hub_dir, spoke_a, spoke_c):
        assert _track(machine, _DROPPED) == (None, _machine(spoke_c)), f"{machine.name}: the kept track is missing"


# ----- controls ------------------------------------------------------------


def test_a_new_machine_with_its_own_tracks_completes_its_first_sync(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_c: Path
) -> None:
    _add_track(spoke_a, _KEPT)
    _sync(spoke_a, hub, "spoke-a")
    _add_track(spoke_c, "trk-new")

    result = _sync(spoke_c, hub, "spoke-c")
    _sync(spoke_a, hub, "spoke-a")

    assert result.accepted >= 1
    for machine in (hub_dir, spoke_a, spoke_c):
        assert _track(machine, "trk-new") == (None, _machine(spoke_c)), f"{machine.name}: a new track did not arrive"


def test_a_restored_hub_is_reseeded_not_refused(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """A restore forgets rows the hub really held; B's copies are how they come back."""
    _add_track(spoke_a, _KEPT)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    _drop_without_tombstone(hub_dir, _KEPT)
    generation.rotate(hub_dir)

    _sync(spoke_b, hub, "spoke-b")

    assert _track(hub_dir, _KEPT) == (None, _machine(spoke_a)), "the re-seed did not restore the forgotten track"


def test_the_hub_refuses_a_push_that_skipped_the_spoke_check(
    hub: _TestClientTransport,
    hub_dir: Path,
    spoke_a: Path,
    spoke_b: Path,
    spoke_c: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fleet_dropped_a_track(hub, hub_dir, spoke_a, spoke_b, spoke_c, keep_watermark=False)
    before = _changelog_size(hub_dir)
    monkeypatch.setattr(client_stale_copy, "refuse_a_stale_copy_push", lambda *_a, **_k: None)

    with pytest.raises(SyncTransportError) as refused:
        _sync(spoke_c, hub, "spoke-c")

    assert refused.value.status_code == 409
    assert refused.value.code == stale_copy.CODE
    assert _track(hub_dir, _DROPPED) is None
    assert _children(hub_dir, _DROPPED) == 0
    # Earlier batches may land; this offer fits one, so nothing moved.
    assert _changelog_size(hub_dir) == before
