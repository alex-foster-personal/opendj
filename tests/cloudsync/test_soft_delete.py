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

import ast
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.reconcile.match import load_track_rows
from apps.reconcile.reacquire import load_playlist_memberships
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.sync_stamp import encode_row_pk
from apps.shared.state.writer import StateWriter
from apps.smartlists.evaluator import evaluate
from apps.spotify.matcher_adapter import load_local_tracks
from apps.stems.cli import _duration_from_state, resolve_audio_path
from apps.sync_hub import client, service
from apps.vocals.cli import Ctx as VocalsCtx
from apps.vocals.cli import load_tracks as vocals_load_tracks
from apps.webui import crate_sync
from apps.webui.server import search_index
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
    # apps/shared/state/normalize_locations.py collapses an NFD/NFC duplicate
    # pair (round 3 finding R3) exactly the way engine._drop_superseded
    # collapses a natural-key duplicate: the loser is hard-deleted because the
    # partial UNIQUE index cannot hold a tombstone and its NFC replacement at
    # once, and the survivor's changelog entry carries the collapse to peers.
    # This IS an allowlisted vacuum path (ADR 08 point 5). LOCATIONS_TABLE is
    # the track_locations delete; changelog prunes its dangling entries in the
    # two (non-synced) changelog tables.
    ("apps/shared/state/normalize_locations.py", "dynamic:LOCATIONS_TABLE"),
    ("apps/shared/state/normalize_locations.py", "dynamic:changelog"),
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


# ----- round 4 (R5): every SELECT of a synced table is deleted_at-honest ----
#
# .planning/cloudsync-round3-adversarial.md finding R5 and Part 3 item 4. The
# round 3 fixes closed five named readers; the round 2 review found the tail by
# grepping "FROM tracks with no deleted_at anywhere". This guard makes that a
# permanent, structural check across ALL eight synced tables: a file that reads
# a synced table must filter deleted_at on a read of that table, OR carry a
# reviewed exemption below. It is the read-side twin of
# test_no_hard_delete_on_a_synced_table_outside_the_allowlist.


def _sql_text(node: ast.AST) -> str | None:
    """The literal SQL a string node carries, or None if it is not one.

    Adjacent string literals Python already merged into one ``Constant``.
    An f-string (``JoinedStr``) has its literal parts kept and every
    ``{placeholder}`` replaced by a marker that cannot read as a table name --
    so a dynamic ``FROM {spec.name}`` is invisible to the FROM regex, which is
    correct: the sync engine reads synced tables dynamically and MUST see
    tombstones. String ``+`` concatenation of two literals is folded too.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(" {} ")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _sql_text(node.left), _sql_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _iter_sql_strings(tree: ast.AST) -> list[str]:
    """Every top-level SQL string in ``tree`` (nested parts not double-counted)."""
    out: list[str] = []

    def visit(node: ast.AST) -> None:
        text = _sql_text(node)
        if text is not None:
            out.append(text)
            return  # a string node: do not descend into its own parts
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return out


_FROM_RE = re.compile(r'\bFROM\s+"?(\w+)"?')

# The subset of _SYNCED_TABLES whose ROWS are the user-visible or sync-visible
# ENTITIES a listing can resurface once tombstoned: a track, a playlist, a
# playlist membership. These are the tables a soft-delete is actually stamped
# onto today (playlists + memberships via StateWriter.delete_playlist; tracks
# via the future track-delete path that round 3 already fixed readers against
# defensively). The other synced tables (track_vendor_ids, track_fields,
# track_locations, sync_policies, playlist_pins) carry a deleted_at column --
# v6 added it uniformly -- but nothing stamps it INDEPENDENTLY of a parent
# entity, and every read of them is a per-entity join scoped by an
# already-filtered parent, so a listing-resurfacing bug is not reachable
# through them. When one of those tables gains its own tombstoning, add it
# here and re-triage its readers.
_ENTITY_TABLES: frozenset[str] = frozenset({
    "tracks", "playlists", "playlist_memberships",
})


def _synced_reads(sql_strings: list[str]) -> tuple[set[str], set[str]]:
    """(entity tables read, entity tables read WITH a deleted_at filter).

    A "read" is a SELECT that names an entity table after FROM. ``deleted_at``
    anywhere in the same statement string counts as filtering every entity
    table that statement reads -- coarse per-statement, but rolled up
    per-(file, table): a file passes for table T when SOME read of T filters
    deleted_at, so a legitimate PK-exact lookup (``WHERE stable_id = ?``)
    alongside a filtered listing does not each need its own exemption. It is
    an UNFILTERED listing with no filtered sibling read that trips this.
    """
    read: set[str] = set()
    filtered: set[str] = set()
    for sql in sql_strings:
        if "SELECT" not in sql.upper():
            continue
        tables = {t for t in _FROM_RE.findall(sql) if t in _ENTITY_TABLES}
        if not tables:
            continue
        read |= tables
        if "deleted_at" in sql:
            filtered |= tables
    return read, filtered


# (relative file, entity table) reads left unfiltered on purpose. Every entry
# is a read that does NOT (and need not) filter deleted_at, with the reason.
# This is the read-side counterpart to _ALLOWED_HARD_DELETES; keep it narrow
# and keep the reasons honest. Writers (StateWriter, the Spotify importer, the
# rekordbox ingest) are NOT here: their entity reads already carry
# ``deleted_at`` -- they read it to reactivate a tombstone -- so they pass the
# guard on their own rather than needing an exemption.
_ALLOWED_UNFILTERED_READS: frozenset[tuple[str, str]] = frozenset({
    # PK-exact lookups for an already-resolved id, not listings. A caller that
    # already holds a stable_id got it from a filtered listing; re-filtering
    # here would only turn a tombstoned-but-referenced row into a confusing
    # "not found" instead of a clear tombstone.
    #   hydration: SELECT content_hash FROM tracks WHERE stable_id = ?
    #   (its listing reads DO filter deleted_at; only this PK lookup does not).
    ("apps/cloud/hydration.py", "tracks"),
    #   locations.pick_playable: SELECT duration_ms, file_path FROM tracks
    #   WHERE stable_id = ? -- a per-track fetch for the audio picker.
    ("apps/shared/state/locations.py", "tracks"),
    #   provenance: SELECT ... FROM tracks WHERE stable_id = ? for a join key
    #   (round 3 R5 judged this one safe explicitly).
    ("apps/shared/state/provenance.py", "tracks"),

    # Migration / DDL SQL. The v4->v6 rebuild and its INSERT ... SELECT copies
    # predate deleted_at semantics and MUST carry every row across the rebuild;
    # filtering would drop tombstones the fleet still needs to converge on.
    ("apps/shared/state/schema.py", "tracks"),
    ("apps/engine_core/store/schema.py", "tracks"),

    # The launcher DB is a DERIVED copy, not state.db. bootstrap_db.py projects
    # state.db INTO the launcher's own sqlite file and already applies
    # WHERE deleted_at IS NULL at that boundary (bootstrap_db.py ~line 283, a
    # dynamic where_sql the AST cannot see as a literal). latency_check.py then
    # reads that already-filtered launcher table, whose FTS mirror does not even
    # carry a deleted_at column. One hop removed from the synced table.
    ("apps/launcher/scripts/bootstrap_db.py", "tracks"),
    ("apps/launcher/scripts/latency_check.py", "tracks"),

    # play_orders reads memberships for ONE already-resolved playlist ordered by
    # position (SELECT stable_id, position FROM playlist_memberships WHERE
    # playlist_id = ?). Membership rows are only ever tombstoned as a set when
    # their whole playlist is deleted (StateWriter edits via hard REPLACE), and
    # a deleted playlist's play order is not requested. When membership-level
    # tombstoning firms up (design_decision_08.md point 8b, open), the owner
    # should add the filter; flagged in the round 4 lane report.
    ("apps/shared/play_orders/store.py", "playlist_memberships"),
})


def test_every_synced_table_read_filters_deleted_at_or_is_allowlisted() -> None:
    """Round 4 R5: the read-side guard. A file that SELECTs a synced table
    must filter ``deleted_at`` on a read of that table, or carry a reviewed
    exemption in ``_ALLOWED_UNFILTERED_READS``. This is what stops 4b's tail
    from silently regrowing a listing that resurfaces a tombstoned row.
    """
    repo_root = Path(__file__).resolve().parents[2]
    unexpected: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in _apps_py_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        read, filtered = _synced_reads(_iter_sql_strings(tree))
        for table in read - filtered:
            seen.add((rel, table))
            if (rel, table) not in _ALLOWED_UNFILTERED_READS:
                unexpected.append((rel, table))

    assert not unexpected, (
        "synced-table read with no deleted_at filter and no exemption: "
        f"{sorted(unexpected)}. Add `WHERE deleted_at IS NULL` to the read "
        "(see apps/webui/server/search_index.py), or -- only if the read must "
        "see tombstones (a writer, the sync layer, an admin dump, a derived "
        "DB) -- add it to _ALLOWED_UNFILTERED_READS with the reason."
    )

    # Keep the exemption list honest: an entry no longer matched by any real
    # read is stale and must be pruned, exactly like _ALLOWED_HARD_DELETES.
    stale = _ALLOWED_UNFILTERED_READS - seen
    assert not stale, (
        f"allowlisted unfiltered read(s) no longer found in source: "
        f"{sorted(stale)}. Remove them from _ALLOWED_UNFILTERED_READS."
    )


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
