"""Year and Label columns read from file tags (issue #10029, rekordbox parity).

Regression one-liners:
  - if a tagged file's year and publisher are not stored as an int and a trimmed string at ingest then broken
  - if an absent or unparseable year, or an empty label, stores anything but null then broken
  - if a track row does not carry year and label, or a row without them is not null, then broken
  - if a tag refresh does not backfill year and label then broken
  - if GET /api/v1/search?q=<label> does not find the label's tracks then broken
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.shared.classical_tags import tag_label, tag_year
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder, tag_refresh
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.fixtures import tagged_audio as ta

_MODIFIED_AT = "2026-11-13T06:00:00.000000Z"
_TAGS = {**ta.STANDARD_TAGS, "date": "2019-05-03", "publisher": "  Hotflush  "}


# ----- helpers


def _writer(conn: sqlite3.Connection) -> StateWriter:
    return StateWriter(conn, bus=FakeEventBus(), actor="year-label-test")


def _import_tagged(tmp_path: Path, tags: dict[str, str]) -> tuple[sqlite3.Connection, str]:
    root = tmp_path / "music"
    root.mkdir()
    ta.make_tagged_audio(root, "mp3-v24", tags=tags)
    conn = state_db.open_rw(tmp_path / "state.db")
    writer = _writer(conn)
    try:
        folder.ingest_folder(writer, [root], dry_run=False)
    finally:
        writer.close()
    (sid,) = conn.execute("SELECT stable_id FROM tracks").fetchone()
    return conn, str(sid)


def _fields(conn: sqlite3.Connection, sid: str) -> dict[str, object]:
    rows = conn.execute(
        "SELECT field_name, value_json FROM track_fields WHERE stable_id = ? AND field_name IN ('year', 'label')",
        (sid,),
    ).fetchall()
    return {str(name): json.loads(value) for name, value in rows}


def _set_eav_field(conn: sqlite3.Connection, stable_id: str, field_name: str, value: object) -> None:
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, confidence, modified_at) "
        "VALUES (?, ?, ?, 'inferred', 0.7, ?)",
        (stable_id, field_name, json.dumps(value), _MODIFIED_AT),
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = _writer(conn)
    try:
        for sid, title in (("s-label", "Lantern Orchard"), ("s-old", "Velvet Static")):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=title,
                artists=["Mira Valen"],
                album=None,
                isrc=None,
                duration_ms=200_000,
                file_path=f"/music/{sid}.mp3",
            )
        _set_eav_field(conn, "s-label", "year", 2019)
        _set_eav_field(conn, "s-label", "label", "Hotflush")
        conn.commit()
    finally:
        writer.close()
        conn.close()
    app = create_app(
        backend=SqliteBackend(path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(path),
        mount_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client


# ----- ingest


@pytest.mark.requires_ffmpeg
def test_folder_import_stores_year_int_and_trimmed_label(tmp_path: Path) -> None:
    """[if] a tagged mp3 with date 2019-05-03 and a padded publisher is imported [then] year 2019 and label Hotflush, [else stop]."""
    conn, sid = _import_tagged(tmp_path, _TAGS)
    assert _fields(conn, sid) == {"year": 2019, "label": "Hotflush"}


@pytest.mark.requires_ffmpeg
def test_folder_import_without_year_or_label_stores_nothing(tmp_path: Path) -> None:
    """[if] a tagged mp3 carries no date and no publisher [then] neither field is stored (reads as null), [else stop]."""
    conn, sid = _import_tagged(tmp_path, dict(ta.STANDARD_TAGS))
    assert _fields(conn, sid) == {}


def test_unparseable_year_and_blank_label_read_as_null() -> None:
    """[if] a tag year is not 4 digits or a publisher is blank [then] tag_year and tag_label answer None, [else stop]."""
    for raw in ("sometime", "20190", "", None, "'19"):
        assert tag_year(SimpleNamespace(year=raw)) is None, raw
    assert tag_year(SimpleNamespace(year="2019-05-03")) == 2019
    assert tag_label(SimpleNamespace(other={"publisher": ["   "]})) is None
    assert tag_label(SimpleNamespace(other={})) is None
    assert tag_label(SimpleNamespace(other={"label": [" Hotflush "]})) == "Hotflush"


@pytest.mark.requires_ffmpeg
def test_tag_refresh_backfills_year_and_label(tmp_path: Path) -> None:
    """[if] a never-read row is tag-refreshed [then] it gains the file's year and label, [else stop]."""
    conn, sid = _import_tagged(tmp_path, _TAGS)
    conn.execute("DELETE FROM track_fields WHERE field_name IN ('year', 'label')")
    conn.execute("UPDATE tracks SET artists_json = '[]', album = NULL, duration_ms = NULL")
    conn.commit()
    (row,) = tag_refresh.blank_rows(conn)
    writer = _writer(conn)
    try:
        assert tag_refresh.refresh_row(writer, row) is True
    finally:
        writer.close()
    assert _fields(conn, sid) == {"year": 2019, "label": "Hotflush"}


# ----- rows and search (agent parity: plain HTTP)


def test_track_rows_carry_year_and_label_null_when_absent(client: TestClient) -> None:
    """[if] GET /tracks and /tracks/{sid} are read [then] the tagged row has year and label and the other null, [else stop]."""
    one = client.get("/api/v1/tracks/s-label").json()
    assert (one["year"], one["label"]) == (2019, "Hotflush")
    items = {item["stable_id"]: item for item in client.get("/api/v1/tracks").json()["items"]}
    assert (items["s-label"]["year"], items["s-label"]["label"]) == (2019, "Hotflush")
    assert (items["s-old"]["year"], items["s-old"]["label"]) == (None, None)


def test_search_by_label_finds_the_labels_tracks(client: TestClient) -> None:
    """[if] GET /api/v1/search?q=Hotflush is asked [then] only the track on that label comes back, with its label, [else stop]."""
    body = client.get("/api/v1/search", params={"q": "Hotflush"}).json()
    assert [item["stable_id"] for item in body["items"]] == ["s-label"]
    assert body["items"][0]["label"] == "Hotflush"
    assert body["items"][0]["year"] == 2019
