"""LIBUX-10046: GET /api/v1/tracks/{stable_id}/related scores by configurable criteria.

[if] an agent asks for related tracks [then] each enabled matching rule scores one point and omitted params use the stored pref, [else stop].

Regression lines:
- if the issue's exact query does not score one point per enabled matching rule then the criteria are ignored
- if bpm_percent 0 or 21 is accepted then the range is unbounded
- if a bare GET ignores the stored library_related_criteria then the cog does nothing
- if a default request opens master.plain.db then the colour reader runs when it is off
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.rb_vendor_pkg.track_colors import bulk_rb_color_ids
from apps.webui.server.routes import track_related
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBUX-10046")

_MODIFIED_AT = "2026-11-08T10:00:00.000000Z"
# sid: (artists, bpm, key, genre, rating, tags, rekordbox ColorID)
_TRACKS: dict[str, tuple[list[str], float, str, str, int, list[str], int]] = {
    "seed": (["Mira Valen"], 120.0, "8A", "house", 3, ["warm"], 2),
    "near": (["Other"], 124.0, "8A", "techno", 3, ["warm"], 2),
    "far": (["Mira Valen"], 126.5, "3B", "house", 1, [], 5),
    "half": (["X & Mira Valen"], 60.5, "9A", "house", 5, ["cold"], 0),
    "none": (["Nobody"], 90.0, "1B", "ambient", 0, [], 0),
}


def _set_eav_field(conn: sqlite3.Connection, stable_id: str, field_name: str, value: object) -> None:
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, confidence, modified_at) "
        "VALUES (?, ?, ?, 'manual', NULL, ?)",
        (stable_id, field_name, json.dumps(value), _MODIFIED_AT),
    )


@pytest.fixture
def related_client(tmp_path: Path) -> Iterator[TestClient]:
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        for sid, (artists, bpm, key, genre, rating, tags, _color) in _TRACKS.items():
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=sid,
                artists=artists,
                album=None,
                isrc=None,
                duration_ms=200_000,
                file_path=f"/music/{sid}.mp3",
            )
            for name, value in (("bpm", bpm), ("key", key), ("genre", genre), ("rating", rating), ("tags", tags)):
                _set_eav_field(conn, sid, name, value)
    finally:
        writer.close()
        conn.close()
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
        state_db_path=str(state_path),
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        yield client


def _scores(body: dict) -> dict[str, tuple[int, list[str]]]:
    return {item["stable_id"]: (item["score"], item["matched"]) for item in body["items"]}


def test_defaults_are_the_9753_rule_set(related_client: TestClient) -> None:
    """[if] a bare GET with no stored pref scores other than bpm 6 percent + key + genre [then] defaults drifted, [else stop]."""
    r = related_client.get("/api/v1/tracks/seed/related")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["criteria"] == {
        "bpm_percent": 6,
        "key": True,
        "genre": True,
        "color": False,
        "rating": False,
        "mytag": False,
        "artist": False,
    }
    assert _scores(body) == {
        "near": (2, ["bpm", "key"]),
        "half": (3, ["bpm", "key", "genre"]),
        "far": (2, ["bpm", "genre"]),
    }
    assert [i["stable_id"] for i in body["items"]] == ["half", "near", "far"]
    assert "seed" not in _scores(body)


def _map_to_rekordbox(tmp_path: Path) -> Path:
    """Give every fixture track a rekordbox mapping and a ColorID in a hand-built master.plain.db."""
    master = tmp_path / "master.plain.db"
    with sqlite3.connect(master) as mconn:
        mconn.execute("CREATE TABLE djmdContent (ID TEXT, ColorID INTEGER, rb_local_deleted INTEGER)")
        mconn.executemany(
            "INSERT INTO djmdContent VALUES (?, ?, 0)", [(f"rb-{sid}", row[6]) for sid, row in _TRACKS.items()]
        )
    state_path = tmp_path / "data" / "state" / "state.db"
    with sqlite3.connect(state_path) as sconn:
        cols = {r[1] for r in sconn.execute("PRAGMA table_info(track_vendor_ids)")}
        extra = ", created_at" if "created_at" in cols else ""
        sconn.executemany(
            f"INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id{extra}) "
            f"VALUES (?, 'rekordbox', ?{', ?' if extra else ''})",
            [(sid, f"rb-{sid}", *([_MODIFIED_AT] if extra else [])) for sid in _TRACKS],
        )
    return master


def test_color_reader_reads_nonzero_color_ids(
    related_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the ColorID reader returns ColorID 0 or misses a mapped track [then] colour match is wrong, [else stop]."""
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", _map_to_rekordbox(tmp_path))
    colors = bulk_rb_color_ids(list(_TRACKS), state_db_path=tmp_path / "data" / "state" / "state.db")
    assert colors == {"seed": 2, "near": 2, "far": 5}


def test_issue_query_scores_each_enabled_rule(related_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the issue's exact query does not score one point per enabled matching rule [then] criteria are ignored, [else stop]."""
    # build_track_rows reads the full rekordbox schema for mapped tracks, so the
    # route gets the colour map from the reader seam (the reader is tested above).
    colors = {sid: row[6] for sid, row in _TRACKS.items() if row[6]}
    monkeypatch.setattr(track_related, "bulk_rb_color_ids", lambda ids, *, state_db_path: colors)
    r = related_client.get("/api/v1/tracks/seed/related?bpm_percent=4&key=1&genre=0&color=1&rating=1&mytag=1&artist=0")
    assert r.status_code == 200, r.text
    scores = _scores(r.json())
    assert scores["near"] == (5, ["bpm", "key", "color", "rating", "mytag"])
    assert scores["half"] == (3, ["bpm", "key", "rating"])
    assert "far" not in scores  # 5.4 percent off at bpm_percent=4, colour 5 != 2, rating lower
    assert "none" not in scores


@pytest.mark.parametrize("query", ["bpm_percent=0", "bpm_percent=21", "key=maybe", "limit=0"])
def test_out_of_range_params_are_422(related_client: TestClient, query: str) -> None:
    """[if] an out-of-range criterion is accepted [then] the range is unbounded, [else stop]."""
    assert related_client.get(f"/api/v1/tracks/seed/related?{query}").status_code == 422


def test_default_request_never_reads_colors(related_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a request with colour off reads ColorIDs [then] master.plain.db is touched for nothing, [else stop]."""

    def _refuse(*_a: object, **_k: object) -> dict[str, int]:
        raise AssertionError("colour reader called with color off")

    monkeypatch.setattr(track_related, "bulk_rb_color_ids", _refuse)
    assert related_client.get("/api/v1/tracks/seed/related").status_code == 200


def test_omitted_params_use_stored_pref(related_client: TestClient) -> None:
    """[if] a bare GET ignores the stored criteria [then] the cog does nothing, [else stop]."""
    put = related_client.put("/api/v1/ui-prefs", json={"library_related_criteria": {"bpm_percent": 3, "genre": False}})
    assert put.status_code == 200, put.text
    body = related_client.get("/api/v1/tracks/seed/related").json()
    assert body["criteria"]["bpm_percent"] == 3 and body["criteria"]["genre"] is False
    assert _scores(body) == {"half": (2, ["bpm", "key"]), "near": (1, ["key"])}
    override = related_client.get("/api/v1/tracks/seed/related?genre=1").json()
    assert override["criteria"]["genre"] is True and "far" in _scores(override)


def test_unknown_seed_404_and_limit_keeps_total(related_client: TestClient) -> None:
    """[if] an unknown seed is not a 404 or limit changes total [then] the contract broke, [else stop]."""
    assert related_client.get("/api/v1/tracks/missing/related").status_code == 404
    body = related_client.get("/api/v1/tracks/seed/related?limit=1").json()
    assert len(body["items"]) == 1 and body["total"] == 3
