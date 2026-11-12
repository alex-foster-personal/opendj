"""Issue #13158: GET /api/v1/lyrics offset must stay inside SQLite signed INTEGER.

Spec: (private path) (Goal, Root cause).

Regression one-liners:
- [if] offset is within signed 64-bit range [then] HTTP 200, [else stop].
- [if] offset exceeds signed 64-bit max [then] HTTP 422 names offset, [else stop].
- [if] offset is below signed 64-bit range [then] HTTP 422 names offset, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.lyrics import store
from apps.lyrics.karaoke_cache import PIPELINE_VERSION
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.routes import lyrics_words as lyrics_words_route
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("SEARCH-NAV-13496")

_SID = "sid-lyrics-offset"
_COMPUTED_AT = "2026-09-01T00:00:00.000000+00:00"
_SQLITE_MAX_SIGNED_INT = lyrics_words_route._SQLITE_MAX_SIGNED_INT


def _validation_fields(response) -> list[str]:
    detail = response.json()["detail"]
    if isinstance(detail, list):
        return [str(part) for err in detail for part in err.get("loc", [])]
    return [str(detail)]


def _seed_minimal(data_dir: Path) -> None:
    conn = state_db.open_rw(data_dir / "state" / "state.db")
    try:
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
            "created_at, updated_at, deleted_at) "
            "VALUES (?, 'inferred', ?, ?, 't0', 't0', NULL)",
            (_SID, "Offset Pin", json.dumps(["Tester"])),
        )
        store.upsert_verdict(
            conn,
            stable_id=_SID,
            verdict="vocal",
            coverage_pct=80.0,
            source="lrclib get",
            language_iso3="eng",
            n_words=1,
            n_lines=1,
            pct_witness_red=0.0,
            pipeline_version=PIPELINE_VERSION,
            words_content_hash=None,
            computed_at=_COMPUTED_AT,
            resurrect=False,
        )
    finally:
        conn.close()


@pytest.fixture
def lyrics_list_client(tmp_path: Path) -> Iterator[TestClient]:
    data_dir = tmp_path / "data"
    state_db_path = data_dir / "state" / "state.db"
    _seed_minimal(data_dir)
    app = create_app(
        backend=SqliteBackend(state_db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        yield client


def test_lyrics_listing_accepts_offset_at_signed_int_max(
    lyrics_list_client: TestClient,
) -> None:
    """[if] offset is SQLite signed INTEGER max [then] HTTP 200 list, [else stop]."""
    response = lyrics_list_client.get(
        "/api/v1/lyrics",
        params={"offset": _SQLITE_MAX_SIGNED_INT},
    )

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_lyrics_listing_rejects_offset_one_past_signed_int_max(
    lyrics_list_client: TestClient,
) -> None:
    """[if] offset is one past signed 64-bit max [then] HTTP 422 offset, [else stop]."""
    response = lyrics_list_client.get(
        "/api/v1/lyrics",
        params={"offset": _SQLITE_MAX_SIGNED_INT + 1},
    )

    assert response.status_code == 422
    assert "offset" in _validation_fields(response)


def test_lyrics_listing_rejects_negative_offset(
    lyrics_list_client: TestClient,
) -> None:
    """[if] offset is negative [then] HTTP 422 names offset, [else stop]."""
    response = lyrics_list_client.get("/api/v1/lyrics", params={"offset": -1})

    assert response.status_code == 422
    assert "offset" in _validation_fields(response)
