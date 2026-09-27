"""CLOUDSYNC-07: the stale-remap replay never leaves a moved row unguarded.

Dropping a stale remap and re-pulling the hub from seq 0 is safe only while
every push still checks the dropped pairs, and it is final only once the
replayed state verifiably agrees with the hub. Any exit short of that must
leave the remap persisted for the next sync's guard. Covers a replay cut off
mid-pull, a process that dies right after the drop commits, and an edit that
lands during the replay's pull. Split from ``test_spoke_recovery.py``, whose
fixtures it reuses.

[if] a replay exit forgets a remap, strands a crash, or pushes a moved row [then] fail, [else stop].
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from apps.sync_hub import client, client_recovery, engine
from tests.cloudsync.test_hub_sync import _open, _sync, _TestClientTransport
from tests.cloudsync.test_spoke_recovery import (
    _LOSER,
    _PLAYLIST,
    _SURVIVOR,
    _a_stale_remap_holding_a_pushed_loser_location,
    _edit_the_moved_location,
    _fields,
    _hub_location_owner,
    _is_live,
    _leave_a_stale_remap_on,
    _members,
    _overall_digest,
    _seed_two_distinct_tracks_on_b,
    _WritesOnceBefore,
)
from tests.cloudsync.test_spoke_recovery_attribution import _remap_of

pytestmark = pytest.mark.requirement("CLOUDSYNC-07")


# ----- helpers ---------------------------------------------------------------


class _DropsTheReplayPull:
    """A hub link that fails the first seq-0 pull, the recovery's replay."""

    def __init__(self, inner: _TestClientTransport) -> None:
        self._inner = inner
        self.dropped = False

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._inner.post(path, payload)

    def get(self, path: str, params: Mapping[str, str | Sequence[str]]) -> dict[str, Any]:
        if not self.dropped and path.endswith("/pull") and params.get("since_seq") == "0":
            self.dropped = True
            raise client.SyncTransportError("connection reset during the replay pull")
        return self._inner.get(path, params)


def _a_bricked_spoke_a(hub: _TestClientTransport, spoke_a: Path, spoke_b: Path) -> None:
    _seed_two_distinct_tracks_on_b(spoke_b)
    _sync(spoke_b, hub, "spoke-b")
    _sync(spoke_a, hub, "spoke-a")
    _leave_a_stale_remap_on(spoke_a)


def _assert_the_loser_landed(spoke_a: Path) -> None:
    conn = _open(spoke_a)
    try:
        assert _is_live(conn, _LOSER), "the owed replay never ran"
        assert _fields(conn, _LOSER) == {"bpm": "100", "key": '"4A"'}
        assert _members(conn, _PLAYLIST) == [_SURVIVOR, _LOSER]
    finally:
        conn.close()


# ----- tests -----------------------------------------------------------------


def test_a_replay_cut_off_mid_pull_keeps_the_remap_and_recovers_next_sync(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """if a cut-off replay forgets the remap its guard needs, or stays bricked, then broken"""
    print("if a cut-off replay forgets the remap its guard needs, or stays bricked, then broken")
    _a_bricked_spoke_a(hub, spoke_a, spoke_b)
    flaky = _DropsTheReplayPull(hub)

    with pytest.raises(client.SyncTransportError, match="replay pull"):
        client.run_sync(spoke_a, "http://hub.invalid", transport=flaky, name="spoke-a")

    assert flaky.dropped, "control: the replay pull was never reached"
    assert _remap_of(spoke_a).get(_LOSER) == _SURVIVOR, "the cut-off replay forgot the remap"

    healed = _sync(spoke_a, hub, "spoke-a")

    assert healed.stale_identity_remaps_dropped == 1, "the next sync did not recover"
    assert _overall_digest(spoke_a) == _overall_digest(hub_dir)
    assert _LOSER not in _remap_of(spoke_a)
    _assert_the_loser_landed(spoke_a)


def test_a_process_that_dies_right_after_the_drop_is_finished_by_the_next_sync(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """if a crash between the drop and the replay leaves the spoke bricked then broken

    Nothing restores the remap when the process dies, so the seq-0 watermark
    committed with the drop is the only thing that carries the debt.
    """
    print("if a crash between the drop and the replay leaves the spoke bricked then broken")
    _a_bricked_spoke_a(hub, spoke_a, spoke_b)
    conn = _open(spoke_a)
    try:
        peers = [str(row[0]) for row in conn.execute("SELECT peer FROM sync_state")]
        assert len(peers) == 1, f"control: expected one hub watermark, found {peers}"
        replay = dataclasses.replace(engine.read_watermark(conn, peers[0]), last_pull_seq=0)
        client_recovery.drop_identity_remaps(conn, [(_LOSER, _SURVIVOR)], replay)
    finally:
        conn.close()
    assert _LOSER not in _remap_of(spoke_a), "control: the crash must leave the remap dropped"

    healed = _sync(spoke_a, hub, "spoke-a")

    assert healed.stale_identity_remaps_dropped == 0
    assert _overall_digest(spoke_a) == _overall_digest(hub_dir)
    _assert_the_loser_landed(spoke_a)


def test_a_moved_row_edited_during_the_replay_pull_is_refused_on_the_settle_round(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """if a settle round after the replay pushes a stale remap's moved row then broken

    The edit lands after the replay offer was fenced, so only a later settle
    round offers it, keyed to the survivor, after the remap was dropped.
    """
    print("if a settle round after the replay pushes a stale remap's moved row then broken")
    _a_stale_remap_holding_a_pushed_loser_location(hub, spoke_a, spoke_b)
    edit = _WritesOnceBefore(
        hub,
        "/pull",
        lambda: _edit_the_moved_location(spoke_a),
        when=lambda params: params.get("since_seq") == "0",
    )

    with pytest.raises(client.SyncDigestMismatch, match="refusing to push"):
        client.run_sync(spoke_a, "http://hub.invalid", transport=edit, name="spoke-a")

    assert edit.fired, "control: the edit never landed on the replay pull"
    assert _hub_location_owner(hub_dir) == _LOSER, "a replay-settle round re-keyed the row"
    assert _remap_of(spoke_a).get(_LOSER) == _SURVIVOR, "the refused recovery forgot the remap"
