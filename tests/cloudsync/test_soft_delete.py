"""Round 2 hardening: soft delete actually used (finding 4a/4b), permanent.

Contract under test: ``specs/design_decision_08.md`` point 5, answering round
1 findings 4a and 4b (``.planning/cloudsync-round1-adversarial.md``).

- finding 4a -- ``StateWriter.delete_playlist`` issued real ``DELETE FROM``
  statements against ``playlists`` and ``playlist_memberships``. On a spoke
  the delete had nothing left to offer, so it silently never propagated; on
  the hub, ``hub_changes_since`` correctly refuses to serve a changelog entry
  for a row that no longer exists, so every OTHER spoke's next ``/pull``
  404s forever. The fix stamps ``deleted_at`` instead of deleting, so the
  deletion itself becomes an ordinary LWW-losable write the merge already
  knows how to carry.
- finding 4b -- nothing outside ``apps/cloud/hydration.py`` and
  ``apps/webui/server/routes/cloudsync.py`` filtered ``deleted_at IS NULL``,
  so a tombstoned row stayed fully visible to every ordinary reader. Fixed
  across the webui backend, ``PlaylistStore``, and the other list/get paths
  audited in this round (see the lane's final report for the full list).

Round 3 addendum (.planning/cloudsync-round2-adversarial.md Part 3 item 8):
round 2 fixed 12 of 19 files that read ``tracks``/``playlists`` without a
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

Acceptance criteria, one test each:
- if deleting a playlist through the real webui write path (``PlaylistStore``,
  the same call ``DELETE /playlists/{id}`` makes) leaves no row behind, the
  delete is still hard, not soft -- broken.
- if that same delete leaves the playlist visible to ``PlaylistStore``'s own
  read path, deleted_at is not being filtered where it matters most --
  broken.
- if the tombstone does not reach a peer over a real hub sync round trip,
  finding 4a's fleet-wide brick is still reachable -- broken.
- if re-inserting a playlist at the same deterministic id (a vendor
  re-ingest replaying an unchanged scan) does not clear the tombstone, a
  local delete becomes permanent even when the vendor never agreed -- broken.
- if any hard ``DELETE FROM <synced table>`` exists in ``apps/`` outside the
  documented whole-list-replace allowlist, finding 4a is unfixed somewhere
  this suite has not looked yet -- broken.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.sync_stamp import encode_row_pk
from apps.shared.state.writer import StateWriter
from apps.smartlists.evaluator import evaluate
from apps.stems.cli import _duration_from_state, resolve_audio_path
from apps.sync_hub import client, service
from apps.vocals.cli import Ctx as VocalsCtx
from apps.vocals.cli import load_tracks as vocals_load_tracks
from apps.webui import crate_sync
from apps.webui.server.backend import NotFoundError
from apps.webui.server.playlist_store import PlaylistStore
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.cloudsync.test_hub_sync import (
    _open,
    _seed_common_track,
    _sync,
    _TestClientTransport,
)

pytestmark = pytest.mark.requirement("CAT-04")


# ----- fixtures --------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    return tmp_path / "spoke-a"


@pytest.fixture
def spoke_b(tmp_path: Path) -> Path:
    return tmp_path / "spoke-b"


@pytest.fixture
def hub(hub_dir: Path):
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


# ----- helpers -----------------------------------------------------------


def _seed_one_track(conn: sqlite3.Connection, stable_id: str) -> None:
    writer = StateWriter(conn, FakeEventBus(), actor="test")
    try:
        writer.upsert_track(
            stable_id=stable_id, stable_id_tier="inferred", title="T",
            artists=["A"], album=None, isrc=None, duration_ms=1000,
            file_path=None,
        )
    finally:
        writer.close()


# ----- 4a: the public delete path tombstones, it does not vanish ---------


def test_delete_playlist_via_the_public_path_tombstones(state_db_path: Path) -> None:
    """PlaylistStore.delete_playlist is what ``DELETE /playlists/{id}``
    calls -- the exact live path finding 4a bricked the fleet through
    (``StateWriter.delete_playlist``, ``writer.py:517-523`` at the time of
    the review). The row and its memberships must survive as tombstones,
    not disappear from the table.
    """
    conn = state_db.open_rw(state_db_path)
    try:
        _seed_one_track(conn, "trk-1")
    finally:
        conn.close()

    store = PlaylistStore(state_db_path, bus=FakeEventBus(), actor="test")
    try:
        created = store.create_playlist("Doomed")
        created = store.replace_memberships(
            created.playlist_id, ["trk-1"], expected_etag=created.etag,
        )
        store.delete_playlist(created.playlist_id, expected_etag=created.etag)
    finally:
        store.close()

    conn = state_db.open_ro(state_db_path)
    try:
        row = conn.execute(
            "SELECT deleted_at FROM playlists WHERE playlist_id = ?",
            (created.playlist_id,),
        ).fetchone()
        assert row is not None, (
            "the playlists row is gone -- delete_playlist hard-deleted it "
            "instead of stamping deleted_at"
        )
        assert row[0] is not None, "playlist row survived but was never tombstoned"

        member_rows = conn.execute(
            "SELECT stable_id, deleted_at FROM playlist_memberships "
            "WHERE playlist_id = ?",
            (created.playlist_id,),
        ).fetchall()
        assert member_rows, (
            "membership rows are gone -- they were hard-deleted instead of "
            "tombstoned alongside the playlist"
        )
        assert all(deleted_at is not None for _sid, deleted_at in member_rows), (
            "membership rows survived but were never stamped deleted_at"
        )
    finally:
        conn.close()


def test_deleted_playlist_hidden_from_the_real_readers(state_db_path: Path) -> None:
    """Finding 4b: the readers that actually serve the webui must filter
    ``deleted_at IS NULL``, not just the two files the round 1 review found
    already doing it.
    """
    conn = state_db.open_rw(state_db_path)
    try:
        _seed_one_track(conn, "trk-1")
    finally:
        conn.close()

    store = PlaylistStore(state_db_path, bus=FakeEventBus(), actor="test")
    try:
        created = store.create_playlist("Doomed")
        created = store.replace_memberships(
            created.playlist_id, ["trk-1"], expected_etag=created.etag,
        )
        store.delete_playlist(created.playlist_id, expected_etag=created.etag)

        with pytest.raises(NotFoundError):
            store.get_playlist_row(created.playlist_id)
    finally:
        store.close()

    backend = SqliteBackend(state_db_path)
    listed_ids = {p.playlist_id for p in backend.list_playlists()}
    assert created.playlist_id not in listed_ids, (
        "list_playlists still returns a tombstoned playlist"
    )
    with pytest.raises(NotFoundError):
        backend.get_playlist(created.playlist_id)


# ----- 4a continued: the tombstone is what sync actually offers ----------


def test_playlist_tombstone_propagates_via_the_sync_engine(
    hub: _TestClientTransport, spoke_a: Path, spoke_b: Path,
) -> None:
    """The reproduction from the review, end to end: a real hub, two real
    migrated spoke DBs, a real ``StateWriter.delete_playlist`` call. If the
    delete were still a hard ``DELETE``, spoke A would have nothing left in
    ``local_changelog`` to offer and spoke B would never see it go -- exactly
    the round 1 finding 4a brick.
    """
    _seed_common_track((spoke_a, spoke_b), "trk-1")

    conn_a = _open(spoke_a)
    try:
        writer = StateWriter(conn_a, FakeEventBus(), actor="test")
        try:
            writer.insert_playlist(
                playlist_id="pl-dead", name="Dead List",
                vendor="webui", vendor_pl_id="pl-dead",
            )
            writer.set_playlist_memberships("pl-dead", ["trk-1"])
        finally:
            writer.close()
    finally:
        conn_a.close()

    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    conn_b = _open(spoke_b)
    try:
        before = conn_b.execute(
            "SELECT deleted_at FROM playlists WHERE playlist_id = ?",
            ("pl-dead",),
        ).fetchone()
        assert before is not None and before[0] is None, (
            "setup failed: the playlist never reached spoke B alive"
        )
    finally:
        conn_b.close()

    conn_a = _open(spoke_a)
    try:
        writer = StateWriter(conn_a, FakeEventBus(), actor="test")
        try:
            assert writer.delete_playlist("pl-dead") is True
        finally:
            writer.close()
    finally:
        conn_a.close()

    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    conn_b = _open(spoke_b)
    try:
        row = conn_b.execute(
            "SELECT deleted_at FROM playlists WHERE playlist_id = ?",
            ("pl-dead",),
        ).fetchone()
        assert row is not None, (
            "the playlist row is gone on the peer instead of tombstoned -- "
            "a hard delete has nothing to offer the changelog"
        )
        assert row[0] is not None, "spoke A's tombstone never reached spoke B"

        member_rows = conn_b.execute(
            "SELECT stable_id, deleted_at FROM playlist_memberships "
            "WHERE playlist_id = ?",
            ("pl-dead",),
        ).fetchall()
        assert member_rows, "membership rows vanished on the peer instead of tombstoning"
        assert all(deleted_at is not None for _sid, deleted_at in member_rows)
    finally:
        conn_b.close()


# ----- reactivation: a delete must be undoable, not a one-way trip -------


def test_reinsert_after_delete_clears_the_tombstone(state_db_path: Path) -> None:
    """A deterministic ``playlist_id`` (``compute_playlist_id``) means a
    vendor re-ingest can land on a row this machine soft-deleted locally.
    ``apps/shared/state/ingest/rekordbox.py`` calls ``insert_playlist`` on
    every scan regardless of whether the row already exists; if that call
    does not clear the tombstone, deleting a rekordbox playlist locally just
    once would hide it forever, even on machines where rekordbox still has
    it.
    """
    conn = state_db.open_rw(state_db_path)
    try:
        writer = StateWriter(conn, FakeEventBus(), actor="test")
        try:
            writer.insert_playlist(
                playlist_id="pl-x", name="Loft",
                vendor="rekordbox", vendor_pl_id="rb-1",
            )
            assert writer.delete_playlist("pl-x") is True

            tombstoned = conn.execute(
                "SELECT deleted_at FROM playlists WHERE playlist_id = ?",
                ("pl-x",),
            ).fetchone()
            assert tombstoned is not None and tombstoned[0] is not None

            # The vendor re-scan replays the identical row -- same name,
            # same identity. This must still count as a change: the row is
            # coming back from the dead, even though nothing about its
            # displayed content differs.
            changed = writer.insert_playlist(
                playlist_id="pl-x", name="Loft",
                vendor="rekordbox", vendor_pl_id="rb-1",
            )
            assert changed is True, (
                "reactivating a tombstoned row with an unchanged name must "
                "still be reported as a change"
            )

            reactivated = conn.execute(
                "SELECT deleted_at FROM playlists WHERE playlist_id = ?",
                ("pl-x",),
            ).fetchone()
            assert reactivated is not None and reactivated[0] is None, (
                "the tombstone survived the reinsert -- the playlist is "
                "permanently invisible even though the vendor still has it"
            )
        finally:
            writer.close()
    finally:
        conn.close()


# ----- 4a's guard: no hard DELETE against a synced table, ever -----------

_SYNCED_TABLES: frozenset[str] = frozenset({
    "tracks",
    "playlists",
    "playlist_memberships",
    "track_vendor_ids",
    "track_fields",
    "track_locations",
    "sync_policies",
    "playlist_pins",
})

# Captures the DELETE target either as an f-string interpolation
# (``DELETE FROM {expr}`` or ``DELETE FROM "{expr}"``, group 1) or as a
# literal/quoted identifier (``DELETE FROM tracks`` / ``DELETE FROM "tbl"``,
# group 2). A literal target is checked against the synced-table set
# directly; a dynamic one cannot be, so every dynamic target is treated as a
# hit regardless of what it is -- apps/sync_hub and apps/reconcile are the
# only places a table name is ever plumbed through a variable, and each such
# site is reviewed once, by hand, below.
_DELETE_RE = re.compile(r'DELETE\s+FROM\s+(?:"?\{([^}]+)\}"?|"?(\w+)"?)')

# (relative file, target) pairs left standing on purpose, target being
# either a resolved table name or "dynamic:<expression>" for an
# f-string-interpolated target this test cannot resolve statically.
#
# The three playlist_memberships entries are all whole-playlist REPLACEs
# (delete every row for one playlist_id, then insert the fresh set) -- the
# same pattern StateWriter.set_playlist_memberships uses and ADR 04 c5
# specifies for how membership travels on the wire ("playlist_memberships
# never appears as a top-level change -- each playlists row carries its
# complete membership bundle instead"). None of these delete a row that
# represents its OWN independent entity the way a playlist or a track does,
# so ADR 08 point 5 does not require them to tombstone. Whether they SHOULD
# anyway (so an undelete of the parent playlist recovers exactly what a
# losing peer held) is design_decision_08.md point 8's open question, not
# decided this round -- this allowlist stays narrow rather than exempting
# the table wholesale.
_ALLOWED_HARD_DELETES: frozenset[tuple[str, str]] = frozenset({
    # Local ingest/webui full-replace: StateWriter.set_playlist_memberships.
    ("apps/shared/state/writer.py", "playlist_memberships"),
    # Spotify importer's own full-replace (Phase 9 predates StateWriter's
    # membership helper).
    ("apps/spotify/state_writer.py", "playlist_memberships"),
    # The sync engine's own wire-apply: apps.sync_hub.engine._replace_members
    # runs only when the parent playlists row wins LWW.
    ("apps/sync_hub/engine.py", "dynamic:MEMBERSHIP_TABLE"),
    # apps.sync_hub.engine._drop_superseded -- the one place a hard DELETE
    # against an arbitrary SYNC_TABLES member is correct by design: two
    # peers minted different natural-key duplicates (round 1 finding 5b's
    # shape), and the partial UNIQUE index cannot hold a tombstone and its
    # replacement at once, so the loser is hard-deleted plus its changelog
    # entries pruned. This IS the "allowlisted vacuum path" ADR 08 point 5
    # anticipates. See the function's own docstring for the full reasoning.
    ("apps/sync_hub/engine.py", "dynamic:spec.name"),
    # Same function, second statement: prunes the dangling hub_changelog /
    # local_changelog entries for the row just dropped above. Neither
    # changelog table is itself a synced table.
    ("apps/sync_hub/engine.py", "dynamic:changelog"),
    # apps/reconcile/remove_track.py cleans up Rekordbox's OWN master.db
    # (ContentID-keyed cascade tables) after pyrekordbox deletes a content
    # row -- a vendor database this module owns outright, not our synced
    # state.db. Same {tbl} target on two lines (ContentID and ID cascades).
    ("apps/reconcile/remove_track.py", "dynamic:tbl"),
})


def _apps_py_files(repo_root: Path) -> list[Path]:
    """Every ``.py`` file under ``apps/``, pruning vendored/build trees."""
    import os

    skip_dirs = {"node_modules", "__pycache__", ".venv", "dist", "build"}
    out: list[Path] = []
    apps_root = repo_root / "apps"
    for dirpath, dirnames, filenames in os.walk(apps_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        out.extend(Path(dirpath) / name for name in filenames if name.endswith(".py"))
    return sorted(out)


def test_no_hard_delete_on_a_synced_table_outside_the_allowlist() -> None:
    """The grep-based guard ADR 08 point 5 asks for: a hard ``DELETE`` on a
    synced table has no way to tell a peer the row is gone (finding 4a).
    Every hit here is either a documented, reviewed exception above, or a
    regression that needs converting to a ``deleted_at`` stamp via
    ``apps.shared.state.sync_stamp``.
    """
    repo_root = Path(__file__).resolve().parents[2]
    hits: list[tuple[str, int, str]] = []
    for path in _apps_py_files(repo_root):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(repo_root).as_posix()
        for match in _DELETE_RE.finditer(text):
            dynamic_expr, literal_name = match.groups()
            if dynamic_expr is not None:
                target = f"dynamic:{dynamic_expr}"
            elif literal_name in _SYNCED_TABLES:
                target = literal_name
            else:
                continue  # a legit non-synced-table delete; not our concern
            lineno = text.count("\n", 0, match.start()) + 1
            hits.append((rel, lineno, target))

    unexpected = [
        (rel, lineno, target) for rel, lineno, target in hits
        if (rel, target) not in _ALLOWED_HARD_DELETES
    ]
    assert not unexpected, (
        "hard DELETE against a synced table outside the ADR 08 point 5 "
        f"allowlist: {unexpected}. Convert it to a deleted_at stamp via "
        "apps.shared.state.sync_stamp.stamp_and_log (see "
        "StateWriter.delete_playlist for the pattern), or -- only if it is "
        "a genuine whole-list replace or vacuum path like the ones already "
        "here -- add it to _ALLOWED_HARD_DELETES with the same reasoning."
    )

    # The allowlist is a claim about what source currently looks like, not
    # just a permission slip. If one of these entries moves or is
    # refactored away, prune it here rather than leave a stale exemption
    # nothing uses.
    seen_pairs = {(rel, target) for rel, _lineno, target in hits}
    stale = _ALLOWED_HARD_DELETES - seen_pairs
    assert not stale, (
        f"allowlisted hard delete(s) no longer found in source: "
        f"{sorted(stale)}. Remove them from _ALLOWED_HARD_DELETES."
    )


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


def test_upsert_track_reactivates_a_tombstoned_row(
    state_db_path: Path,
) -> None:
    """The chokepoint mirror of ``test_reinsert_after_delete_clears_the_
    tombstone`` above, for tracks: ``apps/shared/state/ingest/rekordbox.py``
    calls ``upsert_track`` on every scan regardless of whether the stable_id
    already exists. If a re-ingest silently refreshed a tombstoned row's data
    while leaving ``deleted_at`` set, the row would carry a fresh
    ``updated_at`` and a fresh ``local_changelog`` entry while remaining
    permanently invisible to every deleted_at-filtered reader -- a stamped,
    synced, undead row. Reactivation must clear the tombstone instead, the
    same way ``insert_playlist`` already does.
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
        tombstoned = conn.execute(
            "SELECT deleted_at FROM tracks WHERE stable_id = ?", ("trk-1",),
        ).fetchone()
        assert tombstoned is not None and tombstoned[0] is not None

        # A re-ingest replays the identical row -- same title, same fields.
        # This must still count as a change: the row is coming back from
        # the dead even though nothing about its displayed content differs,
        # exactly as insert_playlist's own reactivation test asserts.
        writer = StateWriter(conn, FakeEventBus(), actor="test")
        try:
            changed = writer.upsert_track(
                stable_id="trk-1", stable_id_tier="inferred", title="Loft",
                artists=["A"], album=None, isrc=None, duration_ms=222_000,
                file_path="/music/loft.mp3",
            )
        finally:
            writer.close()
        assert changed is True, (
            "reactivating a tombstoned track with unchanged data must "
            "still be reported as a change"
        )

        reactivated = conn.execute(
            "SELECT deleted_at FROM tracks WHERE stable_id = ?", ("trk-1",),
        ).fetchone()
        assert reactivated is not None and reactivated[0] is None, (
            "the tombstone survived the re-ingest -- the track is "
            "permanently invisible even though the vendor scan still has it"
        )

        changelog_count = conn.execute(
            "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'tracks' "
            "AND row_pk = ?",
            (encode_row_pk(("trk-1",)),),
        ).fetchone()[0]
        assert changelog_count >= 2, (
            "the reactivation was not logged to local_changelog -- it will "
            "not sync (ADR 08 point 2)"
        )
    finally:
        conn.close()
