"""Shared helpers for rekordbox asset endpoint tests.

Id discovery, the hydrated rekordbox fixture environment, and the
TestClient fixtures used by more than one sibling test module.
Path(__file__) stays one level below the repo root, matching the
helpers' original location.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state.ingest import rekordbox as rb_ingest
from apps.shared.state.writer import StateWriter
from apps.webui.server import rb_vendor
from apps.webui.server.routes.rb_assets import router
from apps.webui.server.sqlite_backend import make_backend

REPO_ROOT = Path(__file__).resolve().parents[1]
UNKNOWN_SID = "0" * 40

# A real playlist-member content id whose FolderPath ``_hydrated_rb_env``
# repoints at a real locked audio fixture, so /playlists available_count has
# at least one genuinely available member to report on any machine.
_AVAILABLE_CONTENT_ID = "183182376"
_AVAILABLE_TRACK_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup" / "src-320.mp3"


# ----- id discovery against the real DBs -------------------------------------


def _rows(db: Path, sql: str, args: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _vendor_to_stable() -> dict[str, str]:
    return {
        str(vid): sid
        for sid, vid in _rows(
            rb_config.STATE_DB,
            "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = 'rekordbox'",
        )
    }


def _discover_ids() -> dict[str, str | None]:
    """Find real stable_ids for each test scenario, at test time."""
    vendor_map = _vendor_to_stable()
    contents = _rows(
        rb_config.MASTER_PLAIN_DB,
        "SELECT ID, FolderPath, ImagePath, AnalysisDataPath FROM djmdContent "
        "WHERE rb_local_deleted = 0 AND FolderPath IS NOT NULL",
    )
    playable = missing_file = None
    for vid, folder, image, anlz in contents:
        sid = vendor_map.get(str(vid))
        if sid is None or not folder or folder.startswith(("tidal:", "soundcloud:", "spotify:")):
            continue
        audio_ok = Path(folder).is_file()
        if playable is None and audio_ok and image and anlz and rb_vendor.resolve_share_path(anlz).is_file():
            playable = sid
        if missing_file is None and not audio_ok:
            missing_file = sid
        if playable and missing_file:
            break

    with_hot_cues = None
    for (vid,) in _rows(
        rb_config.MASTER_PLAIN_DB,
        "SELECT DISTINCT c.ContentID FROM djmdCue c "
        "JOIN djmdContent d ON d.ID = c.ContentID AND d.rb_local_deleted = 0 "
        "WHERE c.rb_local_deleted = 0 AND c.Kind BETWEEN 1 AND 8 "
        "AND d.AnalysisDataPath IS NOT NULL",
    ):
        sid = vendor_map.get(str(vid))
        if sid is not None:
            row = _rows(
                rb_config.MASTER_PLAIN_DB,
                "SELECT AnalysisDataPath FROM djmdContent WHERE ID = ?",
                (str(vid),),
            )[0]
            if row[0] and rb_vendor.resolve_share_path(row[0]).is_file():
                with_hot_cues = sid
                break

    return {
        "playable": playable,
        "missing_file": missing_file,
        "with_hot_cues": with_hot_cues,
    }


def _discover_beatgrid_issue_ids() -> dict[str, str | None]:
    """Real stable_ids for the beatgrid Err-column diagnostic, discovered
    from tracks whose /anlz has already been fetched at least once (so the
    on-disk anlz-cache holds their real parsed PQTZ beats) -- same
    discover-don't-hardcode approach as ``_discover_ids`` above, so this
    works against any real local library, not one specific snapshot."""
    from apps.webui.server import beatgrid_diagnostics as bgd

    dirty = clean = None
    cached_stable_ids: set[str] = set()
    if rb_config.ANLZ_CACHE_DIR.is_dir():
        for path in rb_config.ANLZ_CACHE_DIR.glob("*.json"):
            cached_stable_ids.add(path.stem)
            if dirty is not None and clean is not None:
                continue
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            beats = cached.get("payload", {}).get("beatgrid", {}).get("beats", [])
            if not beats:
                continue
            issue = bgd.detect_beatgrid_issue(beats)
            if issue is not None and dirty is None:
                dirty = path.stem
            elif issue is None and clean is None:
                clean = path.stem

    never_cached = None
    for sid in _vendor_to_stable().values():
        if sid not in cached_stable_ids:
            never_cached = sid
            break

    return {"dirty": dirty, "clean": clean, "never_cached": never_cached}


# ----- fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def _hydrated_rb_env(rb_plain_db_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Path]]:
    """Hydrate a real state.db from the committed/hosted rekordbox fixture.

    ``rb_plain_db_path`` (root ``conftest.py``) resolves and checksum-verifies
    the fixture via ``tests.fixtures._resolver.fixture_path("rekordbox")`` and
    fails closed rather than silently skipping -- Issue #1032's "point it at
    fixture_path('rekordbox')", which this module previously never did.

    The fixture DB is then ingested for real through the production
    ``apps.shared.state.ingest.rekordbox`` pipeline (the same code path as
    ``python -m apps.shared.state.cli ingest-rb``), so every test below
    exercises real vendor-mapping, djmdCue and playlist-membership rows
    rather than a hand-built stub.
    """
    tmp_dir = tmp_path_factory.mktemp("rb_assets_hydrated")
    master_db = tmp_dir / "master.plain.db"
    shutil.copy2(rb_plain_db_path, master_db)

    # The fixture's FolderPath values are the maintainer's own Mac paths
    # (/Users/dev/Music/...) and never exist on a CI runner, so every track
    # would otherwise read available=false everywhere -- true, but untested
    # truth. Repoint one real playlist member at the committed, locked
    # src-320.mp3 fixture (also used by test_rb_artwork_local_track.py) so
    # the available/unavailable split is exercised against real, decodable
    # audio bytes rather than a fabricated blob.
    audio_file = tmp_dir / "available-track.mp3"
    shutil.copy2(_AVAILABLE_TRACK_FIXTURE, audio_file)
    con = sqlite3.connect(master_db)
    try:
        con.execute(
            "UPDATE djmdContent SET FolderPath = ? WHERE ID = ?",
            (str(audio_file), _AVAILABLE_CONTENT_ID),
        )
        con.commit()
    finally:
        con.close()

    state_path = tmp_dir / "state" / "state.db"
    conn = state_db.open_rw(state_path)
    # No bus= override: StateWriter constructs its own real EventBus (the
    # production default) and writer.close() below tears it down, so
    # ingestion exercises the real publish/subscribe/worker-thread path
    # rather than a fake synchronous inline stand-in.
    writer = StateWriter(conn, actor="test-rb-assets")
    try:
        report = rb_ingest.ingest_rb(writer, master_db, dry_run=False)
    finally:
        writer.close()
        conn.close()
    assert report.tracks_inserted > 0, "rekordbox fixture must ingest at least one track"

    originals = {
        name: getattr(rb_config, name)
        for name in (
            "MASTER_PLAIN_DB",
            "STATE_DB",
            "ANLZ_CACHE_DIR",
            "BEATGRID_ISSUE_CACHE_DIR",
        )
    }
    rb_config.MASTER_PLAIN_DB = master_db
    rb_config.STATE_DB = state_path
    rb_config.ANLZ_CACHE_DIR = tmp_dir / "state" / "anlz-cache"
    rb_config.BEATGRID_ISSUE_CACHE_DIR = tmp_dir / "state" / "beatgrid-issue-cache"

    try:
        # #3644 made the playlist tree's available_count a zero-stat read: a
        # path the availability index has never seen reads as pending, not
        # present, until the background refresher stats it. Stat the one
        # on-disk member with a real full scan first, the way an earlier
        # library listing fills the index in production, so the count reads
        # disk truth instead of racing the refresher.
        rb_vendor.bulk_file_exists([str(audio_file)])
        yield {"master": master_db, "state": state_path}
    finally:
        for name, value in originals.items():
            setattr(rb_config, name, value)


@pytest.fixture(scope="module")
def client(_hydrated_rb_env: dict[str, Path]) -> Iterator[TestClient]:
    """Temp app with ONLY the rb-assets router (integrator wires the real one)."""
    app = FastAPI()
    app.state.backend = make_backend(state_db_path=_hydrated_rb_env["state"])
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def ids(_hydrated_rb_env: dict[str, Path]) -> dict[str, str | None]:
    found = _discover_ids()
    if found["playable"] is None:
        pytest.skip(
            "no rekordbox track with on-disk audio + artwork + ANLZ found (share assets not present on this machine)"
        )
    return found


@pytest.fixture(scope="module")
def issue_ids(_hydrated_rb_env: dict[str, Path]) -> dict[str, str | None]:
    return _discover_beatgrid_issue_ids()


def _get_json(client: TestClient, url: str, **kwargs: Any) -> dict[str, Any]:
    resp = client.get(url, **kwargs)
    assert resp.status_code == 200, f"{url} -> {resp.status_code}: {resp.text[:300]}"
    return resp.json()


def _get_json_list(client: TestClient, url: str, **kwargs: Any) -> list[dict[str, Any]]:
    resp = client.get(url, **kwargs)
    assert resp.status_code == 200, f"{url} -> {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, list)
    return body


# ----- vocals (PVDI -- contract item 5) ----------------------------------------


def _discover_vocals_ids() -> dict[str, str | None]:
    """One real stable_id per vocals state, probed from the actual files.

    Full-library probe walk measured at ~1.1 s warm (raw section walk only),
    and it short-circuits once all three states are found.
    """
    vendor_map = _vendor_to_stable()
    found: dict[str, str | None] = {
        "rekordbox": None,
        "no_vocals": None,
        "not_analyzed": None,
    }
    for vid, adp in _rows(
        rb_config.MASTER_PLAIN_DB,
        "SELECT ID, AnalysisDataPath FROM djmdContent "
        "WHERE rb_local_deleted = 0 AND AnalysisDataPath IS NOT NULL "
        "AND AnalysisDataPath != ''",
    ):
        sid = vendor_map.get(str(vid))
        if sid is None:
            continue
        dat = rb_vendor.resolve_share_path(adp)
        if not dat.is_file():
            continue
        vocals = rb_vendor.vocals_payload(dat.with_suffix(".2EX"))
        if found[vocals["status"]] is None:
            found[vocals["status"]] = sid
        if all(found.values()):
            break
    return found


@pytest.fixture(scope="module")
def vocals_ids(_hydrated_rb_env: dict[str, Path]) -> dict[str, str | None]:
    return _discover_vocals_ids()


def _twoex_by_title(title_like: str) -> Path | None:
    for (adp,) in _rows(
        rb_config.MASTER_PLAIN_DB,
        "SELECT AnalysisDataPath FROM djmdContent "
        "WHERE rb_local_deleted = 0 AND Title LIKE ? "
        "AND AnalysisDataPath IS NOT NULL AND AnalysisDataPath != ''",
        (title_like,),
    ):
        twoex = rb_vendor.resolve_share_path(adp).with_suffix(".2EX")
        if twoex.is_file():
            return twoex
    return None


# ----- hydrated listings + availability (contract items 1-4) -------------------


def _row_keys() -> set[str]:
    """The wire contract for one hydrated row: the fields of ``TrackRowOut``.

    Derived from the response model rather than pinned as a literal, because a
    literal copy drifted twice as the model grew (``has_rb_mapping``, then
    ``genre_reason``) and failed on a field the route was correctly emitting.
    What this still catches is a row that DROPS or RENAMES a contract field
    (for example ``exclude_none`` serialization or a hand-built dict).
    """
    from apps.webui.server.models import TrackRowOut

    keys = set(TrackRowOut.model_fields)
    assert {"stable_id", "etag", "preview_b64", "preview_max"} <= keys, keys
    return keys


@pytest.fixture(scope="module")
def full_client(_hydrated_rb_env: dict[str, Path]) -> Iterator[TestClient]:
    """Whole app (tracks + playlists routers) over the real SqliteBackend."""
    from apps.webui.server.app import create_app

    app = create_app(
        backend=make_backend(state_db_path=_hydrated_rb_env["state"]),
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client
