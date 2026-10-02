"""CLOUDSYNC-32: an identity loser the hub has answered for is settled, not held.

The Mac check of PR #4930 (Fri 2 Oct 2026): a library holding its own
duplicate audio under a watched folder printed "152 row(s) held here" on
every sync after the seed, and re-offered the same rows forever
("pushed 4 (accepted 0, rejected 4)"). The seed collapses each duplicate
pair, the hub answers "identity_loser" for the loser, and the spoke drops
the loser row. The watched-folder rescan then re-inserts the loser's file
under its old path-derived id; the persisted hub remap still names it a
loser, so it is held out of every offer, its changelog entry pinned the
push fence one below it, and it was counted as held on every sync.

Every scenario runs real folder ingest of real WAVs, the real hub router
and the real ``run_sync``; a sync that leaves the two sides different
raises ``SyncDigestMismatch``.

- [if] the seed's identity repair push names rejected rows [then] the summary's ``rejected`` counts exactly those rows, [else stop].
- [if] a loser has NOT been answered by the hub [then] it is still held and pins the fence, [else stop].
- [if] the rescan re-inserts a loser the hub answered for [then] it is settled: not offered, not held, no fence pin, [else stop].

Controls for over-correction: a real edit made after the losers settle
still pushes and is accepted, a later restore of a removed track still
reaches the hub, and a row held for a stamp fault still pins the fence
(``tests/cloudsync/test_hub_sync_fence_loss.py``).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from apps.shared.state import sync_stamp
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.sync_hub import client, engine
from apps.sync_hub.engine_identity_map import record_identity_remap
from tests.cloudsync.test_hub_sync import _open, _sync, _TestClientTransport
from tests.cloudsync.test_tombstone_identity_collapse import (
    _id_at,
    _ingest,
    _live_count,
    _remaps,
    _remove,
    _removed,
    _rescan,
    _write_wav,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-32")

#: Duplicate pairs in the library before the seed: each is one recording at
#: two paths, so the seed collapses it and the rescan re-inserts the loser.
_PAIRS = 3


class _DupLibrary:
    """A watched folder holding one unique song and ``_PAIRS`` duplicate pairs."""

    def __init__(self, tmp_path: Path) -> None:
        self.music = tmp_path / "music"
        self.song = self.music / "song.wav"
        _write_wav(self.song, sample=7)
        self.pairs: list[tuple[Path, Path]] = []
        for index in range(_PAIRS):
            first = self.music / f"twin-{index}" / "a.wav"
            _write_wav(first, sample=20 + index)
            second = first.with_name("b.wav")
            shutil.copy2(first, second)
            self.pairs.append((first, second))


def _fence(data_dir: Path) -> tuple[int, int]:
    """``(last_push_seq, local_seq)``: equal once nothing is waiting to be offered."""
    conn = _open(data_dir)
    try:
        peer = str(conn.execute("SELECT peer FROM sync_state").fetchone()[0])
        return engine.read_watermark(conn, peer).last_push_seq, engine.local_seq(conn)
    finally:
        conn.close()


def _seed_then_rescan(hub: _TestClientTransport, spoke: Path, tmp_path: Path) -> tuple[_DupLibrary, client.SyncResult]:
    library = _DupLibrary(tmp_path)
    _ingest(spoke, library.music, picked_by_user=False)
    seed = _sync(spoke, hub, "spoke-a")
    assert _rescan(spoke, library.music).tracks_added == _PAIRS, (
        "premise: the rescan re-inserts every duplicate loser the seed dropped"
    )
    return library, seed


def _pair_ids(data_dir: Path, pair: tuple[Path, Path]) -> tuple[str, str]:
    """``(survivor, loser)`` of one duplicate pair, as the hub answered it.

    Read from the persisted hub remap, never from the pair's order: which
    copy wins is an LWW election over ingest stamps, and ingest walks the
    folder in directory order, which the filesystem decides (``os.walk``
    does not sort). A test that assumed ``a.wav`` survives edited the
    settled loser on a runner that listed ``a.wav`` first.
    """
    ids = {_id_at(data_dir, path) for path in pair}
    losers = ids & set(_remaps(data_dir))
    assert len(losers) == 1, f"premise: the hub answered for exactly one copy of {pair}, got {losers}"
    loser = next(iter(losers))
    return next(iter(ids - losers)), loser


def _comments(data_dir: Path, stable_id: str) -> str | None:
    conn = _open(data_dir)
    try:
        row = conn.execute(
            "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = 'comments'", (stable_id,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _set_comments(data_dir: Path, stable_id: str, text: str) -> None:
    conn = _open(data_dir)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="webui")
    try:
        writer.set_field(stable_id, "comments", text, source="manual", modified_at=sync_stamp.canonical_now())
    finally:
        writer.close()
        conn.close()


def _assert_settled(result: client.SyncResult, label: str) -> None:
    assert (result.pushed, result.rejected, result.quarantined_rows) == (0, 0, 0), (
        f"{label}: pushed {result.pushed}, rejected {result.rejected} "
        f"{result.rejected_rows}, {result.quarantined_rows} row(s) held here"
    )


# ----- the Mac sequence -------------------------------------------------------


def test_the_seed_summary_counts_the_rows_it_names(
    hub: _TestClientTransport, spoke_a: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The seed printed "rejected 0" while its log named 152 identity losers.

    Those were the identity repair's one-row offers of each hub-answered
    loser, which the hub refuses by design: that refusal IS the confirmation.
    They are counted as confirmations, apart from the push's own rejections,
    and the log says so instead of claiming a rejection.
    """
    library = _DupLibrary(tmp_path)
    _ingest(spoke_a, library.music, picked_by_user=False)

    with caplog.at_level(logging.INFO, logger="apps.sync_hub.client"):
        seed = _sync(spoke_a, hub, "spoke-a")

    claimed = [record.getMessage() for record in caplog.records if record.getMessage().startswith("hub rejected")]
    assert seed.rejected == len(seed.rejected_rows) == len(claimed), (
        f"the summary says rejected {seed.rejected}, names {len(seed.rejected_rows)} row(s), "
        f"and the log claims {len(claimed)} rejection(s): {claimed}"
    )
    assert seed.accepted + seed.rejected == seed.pushed, "a decided row is missing from the summary"
    confirmed = [record for record in caplog.records if "confirmed the identity collapse" in record.getMessage()]
    assert seed.identity_repairs == len(confirmed) == _PAIRS, (
        f"{seed.identity_repairs} confirmation(s) counted, {len(confirmed)} logged, {_PAIRS} duplicate pair(s)"
    )


def test_rescanned_identity_losers_settle_and_release_the_fence(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, tmp_path: Path
) -> None:
    """Seed, rescan, remove, rescan, several syncs: the Mac's steady state."""
    library, _seed = _seed_then_rescan(hub, spoke_a, tmp_path)
    losers = [_pair_ids(spoke_a, pair)[1] for pair in library.pairs]

    first = _sync(spoke_a, hub, "spoke-a")
    assert first.quarantined_rows == 0, f"{first.quarantined_rows} settled loser(s) still counted as held"
    removed = _id_at(spoke_a, library.song)
    _remove(spoke_a, removed)
    after_remove = _sync(spoke_a, hub, "spoke-a")
    assert (after_remove.accepted, after_remove.rejected) == (after_remove.pushed, 0)
    _rescan(spoke_a, library.music)

    for attempt in range(3):
        _assert_settled(_sync(spoke_a, hub, "spoke-a"), f"sync {attempt} after the removal")
    last_push_seq, top = _fence(spoke_a)
    assert last_push_seq == top, f"the push fence is pinned at {last_push_seq} below {top}"
    assert _removed(spoke_a, removed) is True and _removed(hub_dir, removed) is True
    # The losers stay in the local library, where they were before the seed;
    # only the survivor of each pair travels.
    assert all(_removed(spoke_a, loser) is False for loser in losers)
    assert all(_removed(hub_dir, loser) is None for loser in losers)
    assert _live_count(hub_dir) == _PAIRS


# ----- controls: real work still moves --------------------------------------


def test_a_real_edit_after_the_losers_settle_still_pushes(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, tmp_path: Path
) -> None:
    """Overshoot control: settling the losers must not swallow genuine edits."""
    library, _seed = _seed_then_rescan(hub, spoke_a, tmp_path)
    _sync(spoke_a, hub, "spoke-a")
    survivor, _loser = _pair_ids(spoke_a, library.pairs[0])
    _set_comments(spoke_a, survivor, "edited after settling")

    edit = _sync(spoke_a, hub, "spoke-a")

    assert edit.pushed > 0 and (edit.accepted, edit.rejected) == (edit.pushed, 0), (
        f"the edit did not reach the hub: {edit}"
    )
    assert "edited after settling" in str(_comments(hub_dir, survivor))
    _assert_settled(_sync(spoke_a, hub, "spoke-a"), "the sync after the edit")


def test_an_edit_on_a_settled_loser_follows_its_survivor_to_the_hub(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, tmp_path: Path
) -> None:
    """The loser is still a row in the local library, so the UI can edit it.

    ``prepare_spoke_identity`` moves that edit onto the survivor before the
    offer. The moved ``track_fields`` row is keyed by ``stable_id``, so the
    move re-keys it; it used to drop the edit's changelog entry with the
    loser's key and log nothing for the new one, so the row sat on the
    survivor locally, was never offered, and the digest raised the ADR 04 c6
    corruption alarm (``track_fields/<survivor>/comments row exists locally
    but not on hub``) on this and every later sync.
    """
    library, _seed = _seed_then_rescan(hub, spoke_a, tmp_path)
    _sync(spoke_a, hub, "spoke-a")
    survivor, loser = _pair_ids(spoke_a, library.pairs[0])
    _set_comments(spoke_a, loser, "edited on the duplicate")

    edit = _sync(spoke_a, hub, "spoke-a")

    assert edit.pushed > 0 and (edit.accepted, edit.rejected) == (edit.pushed, 0), (
        f"the moved edit did not reach the hub: {edit}"
    )
    assert "edited on the duplicate" in str(_comments(hub_dir, survivor))
    assert _comments(spoke_a, loser) is None, "the edit stayed on the settled loser"
    _assert_settled(_sync(spoke_a, hub, "spoke-a"), "the sync after the moved edit")


def test_a_loser_edit_the_survivor_already_answers_is_not_re_offered(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, tmp_path: Path
) -> None:
    """Overshoot control: only a row the move actually wrote is re-offered.

    Where the survivor already holds the field, the move keeps the survivor's
    value and discards the loser's (``INSERT OR IGNORE``), so nothing changed
    on the survivor and the loser's changelog entry must go, not be re-keyed
    onto a row that has nothing new to say.
    """
    library, _seed = _seed_then_rescan(hub, spoke_a, tmp_path)
    _sync(spoke_a, hub, "spoke-a")
    survivor, loser = _pair_ids(spoke_a, library.pairs[0])
    _set_comments(spoke_a, survivor, "the survivor's own")
    _sync(spoke_a, hub, "spoke-a")
    _set_comments(spoke_a, loser, "edited on the duplicate")

    _assert_settled(_sync(spoke_a, hub, "spoke-a"), "the sync after the discarded loser edit")
    assert "the survivor's own" in str(_comments(spoke_a, survivor))
    assert "the survivor's own" in str(_comments(hub_dir, survivor))
    last_push_seq, top = _fence(spoke_a)
    assert last_push_seq == top, f"the push fence is pinned at {last_push_seq} below {top}"


def test_a_restore_after_the_losers_settle_still_reaches_the_hub(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, tmp_path: Path
) -> None:
    """Overshoot control: a fence moved past settled rows still offers a restore."""
    library, _seed = _seed_then_rescan(hub, spoke_a, tmp_path)
    removed = _id_at(spoke_a, library.song)
    _remove(spoke_a, removed)
    _sync(spoke_a, hub, "spoke-a")
    assert _removed(hub_dir, removed) is True

    _ingest(spoke_a, library.music, picked_by_user=True)
    restore = _sync(spoke_a, hub, "spoke-a")

    assert _removed(spoke_a, removed) is False, "premise: the picked re-import restored the track"
    assert _removed(hub_dir, removed) is False, f"the restore never reached the hub: {restore}"
    _assert_settled(_sync(spoke_a, hub, "spoke-a"), "the sync after the restore")


# ----- the rule itself: only a hub answer settles a loser ----------------------


def test_only_a_hub_answer_settles_a_loser_and_its_children(spoke_a: Path, tmp_path: Path) -> None:
    """Overshoot control: an unanswered local loser still pins the fence.

    Until the hub has answered, the local election is this machine's own
    guess, and the loser may yet have to travel (its survivor removed before
    any sync, say), so its changelog entry must stay above the fence. Its
    children (here a real ``track_fields`` edit) follow their parent's
    verdict either way.
    """
    library = _DupLibrary(tmp_path)
    _ingest(spoke_a, library.music, picked_by_user=False)
    first, second = library.pairs[0]
    conn = _open(spoke_a)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="webui")
    try:
        ids = {_id_at(spoke_a, first), _id_at(spoke_a, second)}
        offer = engine.spoke_push(conn, watermark=engine.Watermark(peer="hub", last_sync_at="2026-10-02T00:00:00"))
        held = {row.pk[0] for row in offer.held if row.table == "tracks"}
        loser = next(iter(ids & held))
        writer.set_field(loser, "comments", "edited loser", source="manual", modified_at=sync_stamp.canonical_now())
        unanswered = engine.spoke_push(conn, watermark=engine.Watermark(peer="hub", last_sync_at="2026-10-02T00:00:00"))
        assert {(row.table, row.pk[0]) for row in unanswered.held} >= {("tracks", loser), ("track_fields", loser)}
        assert unanswered.held_seq is not None and unanswered.settled == ()

        record_identity_remap(conn, {}, loser, next(iter(ids - {loser})))
        answered = engine.spoke_push(conn, watermark=engine.Watermark(peer="hub", last_sync_at="2026-10-02T00:00:00"))
    finally:
        writer.close()
        conn.close()

    settled = {(row.table, row.pk[0]) for row in answered.settled}
    assert {("tracks", loser), ("track_fields", loser)} <= settled
    assert {table for table, _pk in settled} == {"tracks", "track_fields", "track_locations"}
    # The other pairs' losers are still unanswered, so they alone stay held.
    before = {(row.table, row.pk[0]) for row in unanswered.held}
    assert settled <= before and {(row.table, row.pk[0]) for row in answered.held} == before - settled
    assert all(change.pk[0] != loser for change in answered.rows if change.table in ("tracks", "track_fields"))
