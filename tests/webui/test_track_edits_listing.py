"""Track listings splice virtual edits directly after their source rows.

[if] an edit loses source availability or index fields [then] listing is invalid, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state import library_edits
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.models import TrackIndexItemOut
from apps.webui.server.routes.tracks import INDEX_ROW_FIELDS
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBEDIT-9179")


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    (tmp_path / "machine-id").write_text("a" * 32, encoding="utf-8")
    writer = StateWriter(conn, FakeEventBus(), actor="test")
    writer.upsert_track(
        stable_id="source-1",
        stable_id_tier="inferred",
        title="Missing source",
        artists=["Artist"],
        album=None,
        isrc=None,
        duration_ms=10_000,
        file_path=str(tmp_path / "not-present.wav"),
    )
    writer.insert_playlist(
        playlist_id="playlist-1",
        name="Set",
        vendor="local",
        vendor_pl_id="playlist-1",
    )
    writer.set_playlist_memberships("playlist-1", ["source-1"])
    library_edits.create_edit(
        writer,
        "listing-test",
        source_track_id="source-1",
        label="Missing cut",
        in_ms=1_000,
        out_ms=7_000,
        fx={"echo": 0.25},
        created_by="tester",
        playlist_id="playlist-1",
    )
    writer.close()
    conn.close()
    monkeypatch.setattr(rb_config, "STATE_DB", path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "wf-cache")
    return path


def _get(path: Path, route: str) -> dict[str, object]:
    app = create_app()
    app.state.backend = SqliteBackend(path)
    app.state.state_db_path = str(path)
    app.state.analysis_db_path = str(path)
    with TestClient(app) as client:
        response = client.get(f"{route}?include_non_music=true")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("route", ["/api/v1/tracks", "/api/v1/tracks/index"])
def test_edits_follow_sources_and_inherit_missing_file_state(state_path: Path, route: str) -> None:
    body = _get(state_path, route)
    rows = body["items"]
    assert isinstance(rows, list)
    assert len(rows) == 2
    source, edit = rows
    assert source["is_edit"] is False
    assert edit["is_edit"] is True
    assert edit["stable_id"] == source["stable_id"] == "source-1"
    assert edit["file_exists"] == source["file_exists"] is False
    assert edit["file_availability"] == source["file_availability"]
    assert edit["file_path"] == source["file_path"]
    assert (edit["in_ms"], edit["out_ms"], edit["fx"]) == (
        1_000,
        7_000,
        {"echo": 0.25},
    )


def test_index_edit_rows_supply_every_declared_field(state_path: Path) -> None:
    rows = _get(state_path, "/api/v1/tracks/index")["items"]
    assert isinstance(rows, list)
    edit = rows[1]
    wire = set(edit)
    declared = set(INDEX_ROW_FIELDS)
    assert wire <= declared
    # compact_listing_analysis_wire drops a null key-guess lane; the index
    # dict still named those fields before serialize.
    assert declared - wire <= {"key_guess", "key_guess_confidence", "key_source"}
    TrackIndexItemOut.model_validate(edit)


def test_playlist_membership_names_and_hydrates_the_edit(state_path: Path) -> None:
    body = _get(state_path, "/api/v1/playlists/playlist-1/tracks")
    rows = body["tracks"]
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0]["edit_id"] is None
    assert rows[1]["edit_id"]
    assert rows[1]["is_edit"] is True
    assert (rows[1]["in_ms"], rows[1]["out_ms"], rows[1]["fx"]) == (
        1_000,
        7_000,
        {"echo": 0.25},
    )
