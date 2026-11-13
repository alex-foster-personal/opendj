"""LATENCY-9993: GET /tracks/{id}?include=hot_cues embeds the hot cue slots.

A deck load read the track and its hot cue slots in two requests against a
single-worker engine; the slots now ride on the track payload when asked for.

[if] a track is read with include=hot_cues [then] it carries the hot-cues list, [else stop].

Regression lines:
  if include=hot_cues differs from GET /tracks/{id}/hot-cues then broken
  if a read without include gains a hot_cues key then broken
  if an unknown include value is not a 422 naming hot_cues then broken

-Claude
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LATENCY-9993")

SID = "a" * 40
STAMP = "2026-11-13T00:00:00+00:00"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An app over one local track (no rekordbox mapping) with an empty own cue store."""
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True)
    conn = state_db.open_rw(db_path)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, file_path, duration_ms, "
        "created_at, updated_at) VALUES (?, 'inferred', 't', '[\"A\"]', '/music/x.mp3', 300000, ?, ?)",
        (SID, STAMP, STAMP),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(rb_config, "STATE_DB", db_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "missing-master.db")
    app = create_app(
        backend=SqliteBackend(db_path), state_db_path=str(db_path), mount_frontend=False, enable_cors=False
    )
    with TestClient(app) as test_client:
        yield test_client


def _save_three_slots(client: TestClient) -> list[dict]:
    """Save hot cues A, B and C through the real PUT route and return the slot list."""
    url = f"/api/v1/tracks/{SID}/hot-cues"
    for slot, in_ms in (("A", 1_000), ("B", 2_000), ("C", 3_000)):
        revision = next(row["revision"] for row in client.get(url).json() if row["slot"] == slot)
        put = client.put(f"{url}/{slot}", json={"in_ms": in_ms}, headers={"If-Match": revision})
        assert put.status_code == 200, put.text
    slots = client.get(url).json()
    assert [row["slot"] for row in slots if row["cue"] is not None] == ["A", "B", "C"]
    return slots


def test_include_hot_cues_embeds_the_hot_cues_list(client: TestClient) -> None:
    """[if] include=hot_cues [then] hot_cues equals GET /hot-cues for a three-slot track, [else stop]."""
    slots = _save_three_slots(client)
    response = client.get(f"/api/v1/tracks/{SID}", params={"include": "hot_cues"})
    assert response.status_code == 200, response.text
    assert response.json()["hot_cues"] == slots


def test_without_include_the_body_is_unchanged(client: TestClient) -> None:
    """[if] no include [then] there is no hot_cues key and the body is the plain read minus hot_cues, [else stop]."""
    _save_three_slots(client)
    plain = client.get(f"/api/v1/tracks/{SID}")
    assert plain.status_code == 200, plain.text
    assert "hot_cues" not in plain.json()
    embedded = client.get(f"/api/v1/tracks/{SID}", params={"include": "hot_cues"}).json()
    embedded.pop("hot_cues")
    assert embedded == plain.json()


def test_unknown_include_is_422_naming_allowed_values(client: TestClient) -> None:
    """[if] include names an unknown block [then] 422 naming hot_cues, [else stop]."""
    response = client.get(f"/api/v1/tracks/{SID}", params={"include": "anlz"})
    assert response.status_code == 422, response.text
    assert "hot_cues" in response.text
