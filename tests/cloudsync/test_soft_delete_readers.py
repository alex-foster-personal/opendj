"""Round 2/3/4 hardening: readers proven, behaviorally, against a tombstone.

Split out of ``test_soft_delete.py`` (round 4 quality-gate ratchet: that
file crossed 600 lines). Same contract (``specs/design_decision_08.md``
point 5); the write-side ("4a" -- the delete itself, and the grep-based
guard against a hard ``DELETE`` on a synced table) stayed there. The
STRUCTURAL read-side guard (finding R5's
``test_every_synced_table_read_filters_deleted_at_or_is_allowlisted``) split
further, into ``test_soft_delete_read_guard.py`` (this file was still over
600 lines with it included) -- this file is the read-side proven
BEHAVIORALLY: nine named readers, each seeded with a live and a tombstoned
row, each asserted to surface only the live one.

Round 3 (.planning/cloudsync-round2-adversarial.md Part 3 item 8): round 2
fixed 12 of 19 files that read ``tracks``/``playlists`` without a
``deleted_at`` filter and left five outstanding --
``apps/smartlists/evaluator.py``, ``apps/stems/cli.py``,
``apps/vocals/cli.py``, ``apps/webui/crate_sync.py``, and
``apps/shared/state/ingest/rekordbox.py``. Those five are covered below, plus
a track-level mirror of the playlist reactivation test: nothing currently
tombstones a live ``tracks`` row (the finding is defense-in-depth for when
track-level delete lands per design_decision_08.md point 8), but
``StateWriter.upsert_track`` must not silently refresh a tombstoned row's
data while leaving it dead -- the same trap ``insert_playlist`` already
closed.

Round 4 finding R5: the round 3 fixes closed five named readers; round 2's
own review found the tail by grepping "FROM tracks with no deleted_at
anywhere". ``test_soft_delete_read_guard.py`` makes that a permanent,
structural check; the four tests here prove the same finding behaviorally,
from R5's exact reproduction (a tombstoned track still searchable in the
webui) through the three siblings it named.

Acceptance criteria, one test each:
- if any of the five round-3 readers (smartlist, stems, vocals, crate_sync,
  the track-reactivation chokepoint) surfaces or refreshes a tombstoned row,
  4b's tail is back -- broken.
- if webui search, the Spotify matcher, the link-repair classifier, or the
  reacquire worklist surfaces a tombstoned track/playlist, R5's four named
  readers have regressed -- broken.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.reconcile.match import load_track_rows
from apps.reconcile.reacquire import load_playlist_memberships
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.sync_stamp import encode_row_pk
from apps.shared.state.writer import StateWriter
from apps.shared.state.writer_tracks import TrackRemovedError
from apps.smartlists.evaluator import evaluate
from apps.spotify.matcher_adapter import load_local_tracks
from apps.stems.cli import _duration_from_state, resolve_audio_path
from apps.vocals.cli import Ctx as VocalsCtx
from apps.vocals.cli import load_tracks as vocals_load_tracks
from apps.webui import crate_sync
from apps.webui.server import search_index

pytestmark = pytest.mark.requirement("CAT-04")


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


# ----- round 3: 4b's five remaining readers -------------------------------
#
# .planning/cloudsync-round2-adversarial.md Part 3 item 8. Nothing currently
# tombstones a live ``tracks`` row (see the module docstring's round 3
# addendum), so each test below stamps ``deleted_at`` directly with raw SQL
# to simulate the future track-delete path these readers must already be
# honest against.

_EPOCH_LATER = "2026-08-31T12:00:00.000000+00:00"


def _seed_track_with_file(
    conn: sqlite3.Connection, stable_id: str, *, file_path: str | None
) -> None:
    writer = StateWriter(conn, FakeEventBus(), actor="test")
    try:
        writer.upsert_track(
            stable_id=stable_id, stable_id_tier="inferred", title="T",
            artists=["A"], album=None, isrc=None, duration_ms=222_000,
            file_path=file_path,
        )
    finally:
        writer.close()


def _tombstone_track(conn: sqlite3.Connection, stable_id: str) -> None:
    conn.execute(
        "UPDATE tracks SET deleted_at = ? WHERE stable_id = ?",
        (_EPOCH_LATER, stable_id),
    )


def test_deleted_track_hidden_from_smartlist_evaluator(
    state_db_path: Path,
) -> None:
    """``apps/smartlists/evaluator.py`` must not resurface a tombstoned
    track in a smartlist -- round 2 left it unfiltered.
    """
    conn = state_db.open_rw(state_db_path)
    try:
        _seed_track_with_file(conn, "live-1", file_path="/music/live.mp3")
        _seed_track_with_file(conn, "dead-1", file_path="/music/dead.mp3")
        _tombstone_track(conn, "dead-1")

        rule = {"field": "added_date", "op": ">=", "value": "2000-01-01T00:00:00+00:00"}
        result = evaluate(rule, conn)
        assert result == ["live-1"], (
            f"evaluate() returned {result!r} -- a tombstoned track is still "
            "visible to the smartlist compiler"
        )
    finally:
        conn.close()


def test_deleted_track_hidden_from_stems_cli_lookups(
    state_db_path: Path, tmp_path: Path
) -> None:
    """``apps/stems/cli.py``'s ``resolve_audio_path``/``_duration_from_state``
    must refuse a tombstoned stable_id the same way they refuse an unknown
    one -- round 2 left both unfiltered.
    """
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    audio = tmp_path / "dead.mp3"
    audio.write_bytes(b"audio-bytes")

    conn = state_db.open_rw(data_dir / "state" / "state.db")
    try:
        _seed_track_with_file(conn, "dead-1", file_path=str(audio))
        _tombstone_track(conn, "dead-1")
    finally:
        conn.close()

    with pytest.raises(FileNotFoundError, match="dead-1"):
        resolve_audio_path(data_dir, "dead-1")
    with pytest.raises(SystemExit, match="dead-1"):
        _duration_from_state(data_dir, "dead-1")


def test_deleted_track_hidden_from_vocals_load_tracks(
    tmp_path: Path,
) -> None:
    """``apps/vocals/cli.py``'s ``load_tracks`` must exclude a tombstoned
    track from the trickle queue -- round 2 left it unfiltered.
    """
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)

    conn = state_db.open_rw(data_dir / "state" / "state.db")
    try:
        for sid, vendor_id in (("live-1", "v-live"), ("dead-1", "v-dead")):
            _seed_track_with_file(conn, sid, file_path=None)
            writer = StateWriter(conn, FakeEventBus(), actor="test")
            try:
                writer.set_vendor_id(sid, "rekordbox", vendor_id)
            finally:
                writer.close()
        _tombstone_track(conn, "dead-1")
    finally:
        conn.close()

    master = sqlite3.connect(data_dir / "master.plain.db")
    try:
        master.execute(
            "CREATE TABLE djmdContent (ID TEXT, Title TEXT, Length INTEGER, "
            "FolderPath TEXT, AnalysisDataPath TEXT, rb_local_deleted INTEGER)"
        )
        master.executemany(
            "INSERT INTO djmdContent VALUES (?, ?, ?, ?, ?, 0)",
            [
                ("v-live", "Live", 200_000, None, None),
                ("v-dead", "Dead", 200_000, None, None),
            ],
        )
        master.commit()
    finally:
        master.close()

    tracks = vocals_load_tracks(VocalsCtx(data_dir=data_dir), None)
    ids = {t.stable_id for t in tracks}
    assert ids == {"live-1"}, (
        f"load_tracks returned {ids!r} -- a tombstoned track is still "
        "queued for vocal extraction"
    )


def test_deleted_track_hidden_from_crate_sync_collect_plan(
    tmp_path: Path,
) -> None:
    """``apps/webui/crate_sync.py``'s ``collect_plan`` must not copy a
    tombstoned track's audio onto the replica crate -- round 2 left the
    unscoped ``FROM tracks`` read unfiltered.
    """
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    source_root = tmp_path / "owner"
    crate_root = tmp_path / "crate"
    live_audio = source_root / "live.mp3"
    dead_audio = source_root / "dead.mp3"
    source_root.mkdir()
    live_audio.write_bytes(b"live-bytes")
    dead_audio.write_bytes(b"dead-bytes")

    conn = state_db.open_rw(data_dir / "state" / "state.db")
    try:
        _seed_track_with_file(conn, "live-1", file_path=str(live_audio))
        _seed_track_with_file(conn, "dead-1", file_path=str(dead_audio))
        _tombstone_track(conn, "dead-1")
    finally:
        conn.close()

    plan = crate_sync.collect_plan(
        state_db=data_dir / "state" / "state.db",
        master_db=None,
        crate_root=crate_root,
        user_maps=((str(source_root), str(crate_root / "mapped")),),
    )
    synced_ids = {sid for f in plan.files for sid in f.stable_ids}
    assert synced_ids == {"live-1"}, (
        f"collect_plan staged {synced_ids!r} -- a tombstoned track is still "
        "planned for the replica crate"
    )


def test_upsert_track_never_rewrites_a_tombstoned_row(
    state_db_path: Path,
) -> None:
    """``apps/shared/state/ingest/rekordbox.py`` calls ``upsert_track`` on every
    scan regardless of whether the stable_id already exists. Two wrong answers
    are possible on a row the user removed, and this pins both out (LIBM-140):

    * refresh the data and leave ``deleted_at`` set: a stamped, synced, undead
      row, invisible to every deleted_at-filtered reader;
    * clear the tombstone: what this test asserted until LIBM-140, and the
      reason every removed track came back on the next rekordbox import.

    The row is left byte-identical and unlogged, and the call raises so a
    caller that forgot to ask ``deleted_tracks.find_deleted_match`` first is
    loud. ``insert_playlist`` still reactivates a playlist; tracks do not.
    """
    conn = state_db.open_rw(state_db_path)
    try:
        writer = StateWriter(conn, FakeEventBus(), actor="test")
        try:
            writer.upsert_track(
                stable_id="trk-1", stable_id_tier="inferred", title="Loft",
                artists=["A"], album=None, isrc=None, duration_ms=222_000,
                file_path="/music/loft.mp3",
            )
        finally:
            writer.close()

        _tombstone_track(conn, "trk-1")
        tombstoned = conn.execute("SELECT * FROM tracks WHERE stable_id = ?", ("trk-1",)).fetchone()
        assert tombstoned is not None
        changelog_before = conn.execute(
            "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'tracks' AND row_pk = ?",
            (encode_row_pk(("trk-1",)),),
        ).fetchone()[0]

        writer = StateWriter(conn, FakeEventBus(), actor="test")
        try:
            for title in ("Loft", "Loft (retagged)"):
                with pytest.raises(TrackRemovedError):
                    writer.upsert_track(
                        stable_id="trk-1", stable_id_tier="inferred", title=title,
                        artists=["A"], album=None, isrc=None, duration_ms=222_000,
                        file_path="/music/loft.mp3",
                    )
        finally:
            writer.close()

        after = conn.execute("SELECT * FROM tracks WHERE stable_id = ?", ("trk-1",)).fetchone()
        assert tuple(after) == tuple(tombstoned), "a re-ingest rewrote a removed track"
        changelog_after = conn.execute(
            "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'tracks' AND row_pk = ?",
            (encode_row_pk(("trk-1",)),),
        ).fetchone()[0]
        assert changelog_after == changelog_before, (
            "a refused write still logged a change, which would sync a no-op "
            "stamp over the tombstone"
        )
    finally:
        conn.close()


# ----- round 4 (R5): the four readers named in finding R5, behaviorally -----
#
# The structural guard above proves the SQL filters; these prove the behavior.
# Finding R5's reproduction was user-visible: `webui search hits=['trk-live',
# 'trk-dead']` -- a track soft-deleted on one machine, searchable on every
# other machine's webui. Each test below tombstones a row and asserts the
# reader no longer surfaces it.


def _seed_titled_track(
    conn: sqlite3.Connection, stable_id: str, title: str, *, isrc: str | None = None
) -> None:
    writer = StateWriter(conn, FakeEventBus(), actor="test")
    try:
        writer.upsert_track(
            stable_id=stable_id, stable_id_tier="inferred", title=title,
            artists=["A"], album=None, isrc=isrc, duration_ms=222_000,
            file_path=None,
        )
    finally:
        writer.close()


def test_deleted_track_hidden_from_webui_search(state_db_path: Path) -> None:
    """apps/webui/server/search_index.py -- the R5 reproduction verbatim: a
    tombstoned track must not appear in the FTS search results the webui
    serves on every machine.
    """
    conn = state_db.open_rw(state_db_path)
    try:
        _seed_titled_track(conn, "trk-live", "Anthem Live")
        _seed_titled_track(conn, "trk-dead", "Anthem Dead")
        _tombstone_track(conn, "trk-dead")
    finally:
        conn.close()

    hits, total = search_index.search(state_db_path, "anthem", limit=10)
    ids = {stable_id for stable_id, _ctx in hits}
    assert ids == {"trk-live"}, (
        f"search returned {ids!r} -- a tombstoned track is still searchable "
        "(finding R5's exact reproduction)"
    )
    assert total == 1


def test_deleted_track_hidden_from_spotify_matcher_targets(
    state_db_path: Path,
) -> None:
    """apps/spotify/matcher_adapter.py -- a Spotify import must not match
    against a tombstoned local track.
    """
    conn = state_db.open_rw(state_db_path)
    try:
        _seed_titled_track(conn, "trk-live", "Live", isrc="USxx11111111")
        _seed_titled_track(conn, "trk-dead", "Dead", isrc="USxx22222222")
        _tombstone_track(conn, "trk-dead")
        targets = load_local_tracks(conn)
    finally:
        conn.close()
    ids = {t.stable_id for t in targets}
    assert ids == {"trk-live"}, (
        f"load_local_tracks returned {ids!r} -- a tombstoned track is still a "
        "match target for the Spotify importer"
    )


def test_deleted_track_hidden_from_reconcile_load_track_rows(
    tmp_path: Path,
) -> None:
    """apps/reconcile/match.py -- the link-repair classifier must not bucket a
    tombstoned track (it would land in a relink plan or a reacquire worklist).
    """
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True)
    conn = state_db.open_rw(db_path)
    try:
        _seed_titled_track(conn, "trk-live", "Live")
        _seed_titled_track(conn, "trk-dead", "Dead")
        _tombstone_track(conn, "trk-dead")
    finally:
        conn.close()

    rows = load_track_rows(db_path, rb_db=None)
    ids = {row.stable_id for row in rows}
    assert ids == {"trk-live"}, (
        f"load_track_rows returned {ids!r} -- a tombstoned track is still "
        "classified for link repair"
    )


def test_deleted_playlist_hidden_from_reacquire_memberships(
    tmp_path: Path,
) -> None:
    """apps/reconcile/reacquire.py -- a tombstoned playlist must not lend its
    name to a track's want-count in the reacquisition worklist.
    """
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True)
    conn = state_db.open_rw(db_path)
    try:
        _seed_titled_track(conn, "trk-1", "Wanted")
        writer = StateWriter(conn, FakeEventBus(), actor="test")
        try:
            writer.insert_playlist(
                playlist_id="pl-live", name="Live List",
                vendor="webui", vendor_pl_id="pl-live",
            )
            writer.set_playlist_memberships("pl-live", ["trk-1"])
            writer.insert_playlist(
                playlist_id="pl-dead", name="Dead List",
                vendor="webui", vendor_pl_id="pl-dead",
            )
            writer.set_playlist_memberships("pl-dead", ["trk-1"])
            assert writer.delete_playlist("pl-dead") is True
        finally:
            writer.close()
    finally:
        conn.close()

    memberships = load_playlist_memberships(db_path)
    assert memberships.get("trk-1") == ["Live List"], (
        f"reacquire saw {memberships.get('trk-1')!r} -- a tombstoned playlist "
        "still inflates the want-count"
    )
