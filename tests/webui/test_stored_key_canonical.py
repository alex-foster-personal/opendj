"""A stored key in any notation is served as canonical Camelot (Sat 24 Oct 2026).

the reporting operator's library mixed rekordbox's Camelot "8A" with its spelled "Am", "F#m" and
"Fmaj" in one table, because ``_row_to_track`` passed the stored string
through. Regression lines:
- if a spelled key reaches the listing un-canonicalised then rows mix notations
- if the no-key sentinel "All" is served as a key then the cell shows a non-key
- if an unparseable key is rewritten or dropped then the data problem hides
- if the ?key= filter compares raw strings then "Am" stops finding "8A" rows
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.backend import TrackFilter
from apps.webui.server.routes.tracks import parse_key_query
from apps.webui.server.sqlite_backend import SqliteBackend

ISO = "2026-10-24T10:00:00.000000Z"
STORED = {
    "sid-camelot": ("8A", "8A"),
    "sid-musical": ("Am", "8A"),
    "sid-major": ("Fmaj", "7B"),
    "sid-openkey": ("1m", "8A"),
    "sid-sentinel": ("All", None),
    "sid-junk": ("H#", "H#"),
    # Non-string stored values (review P1, PR #7483): coerced to text first,
    # so the track and its provenance agree.
    "sid-numeric-zero": (0, None),
    "sid-numeric-junk": (13, "13"),
}


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "state" / "state.db"
    conn = state_db.open_rw(path)
    try:
        for sid, (stored, _) in STORED.items():
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
                "  created_at, updated_at) VALUES (?, 'inferred', ?, '[]', ?, ?)",
                (sid, sid, ISO, ISO),
            )
            conn.execute(
                "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
                "  confidence, modified_at) VALUES (?, 'key', ?, 'rekordbox', 1.0, ?)",
                (sid, json.dumps(stored), ISO),
            )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def client(state_path: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
        state_db_path=str(state_path),
    )
    app.state.data_dir = state_path.parent.parent
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("sid", sorted(STORED))
def test_track_serves_canonical_key(state_path: Path, sid: str) -> None:
    """[if] Track.key is the stored spelling [then] notations mix, [else stop]."""
    track = SqliteBackend(state_path).get_track(sid)
    assert track.key == STORED[sid][1]
    assert track.provenance["key"].value == STORED[sid][1]


def test_listing_serves_one_notation(client: TestClient) -> None:
    """[if] GET /tracks carries a spelled key [then] the table mixes notations, [else stop]."""
    response = client.get("/api/v1/tracks", params={"limit": 50})
    assert response.status_code == 200, response.text
    served = {item["stable_id"]: item["key"] for item in response.json()["items"]}
    assert served == {sid: want for sid, (_, want) in STORED.items()}


def test_key_filter_matches_any_notation(state_path: Path) -> None:
    """[if] ?key=Am misses rows stored as 8A or 1m [then] the filter lies, [else stop]."""
    page = SqliteBackend(state_path).list_tracks(TrackFilter(keys=parse_key_query(["Am"]), limit=50))
    assert sorted(t.stable_id for t in page.items) == ["sid-camelot", "sid-musical", "sid-openkey"]
