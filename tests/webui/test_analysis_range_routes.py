"""One track's own BPM range over HTTP (ANALYSIS-11723).

Hermetic: a tmp state.db through the real migrations and the real app. Nothing is mocked.

Regression one-liners:
  - if `PUT .../analysis-range {"range": "93-186"}` does not store the row then broken
  - if the producer's range resolution ignores the stored range, or the global band moves off 70-180, then broken
  - if `DELETE` does not restore the global band then broken
  - if an unknown range is not 422 ANALYSIS_RANGE_INVALID (and writes nothing) then broken
  - if the frontend's preset list drifts from the server's then the picker offers a 422: broken

[if] HTTP cannot set clear or validate per-track BPM range [then] re-analyze grid is broken, [else stop].

-Claude
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis.backends.range_hint import resolve_bpm_range
from apps.analysis_beatgrid import bpm
from apps.shared.state import analysis_range
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("ANALYSIS-11723")
SID = "d" * 40
FRONTEND_RANGES = Path(__file__).resolve().parents[2] / "apps/webui/frontend/src/lib/rb/reanalyze-grid.ts"


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state" / "state.db"
    path.parent.mkdir(parents=True)
    conn = state_db.open_rw(path)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, file_path, created_at, updated_at) "
        "VALUES (?, 'inferred', 't', '[]', '/x.wav', '2026-01-01', '2026-01-01')",
        (SID,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(rb_config, "STATE_DB", path)
    return path


@pytest.fixture
def client(state_path: Path) -> Iterator[TestClient]:
    app: Any = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_path),
        mount_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def _stored(state_path: Path) -> tuple[str | None, str | None]:
    conn = state_db.open_rw(state_path)
    try:
        row = conn.execute(
            "SELECT source FROM track_fields WHERE stable_id = ? AND field_name = ?", (SID, analysis_range.FIELD)
        ).fetchone()
        return analysis_range.read(conn, SID), (row[0] if row else None)
    finally:
        conn.close()


def test_put_stores_the_range_and_the_producer_resolves_it(client: TestClient, state_path: Path) -> None:
    path = f"/api/v1/tracks/{SID}/analysis-range"
    assert resolve_bpm_range(SID, state_path) == (70.0, 180.0)
    answer = client.put(path, json={"range": "93-186"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["range"] == answer.json()["effective"] == "93-186"
    assert _stored(state_path) == ("93-186", "webui")
    assert resolve_bpm_range(SID, state_path) == (93.0, 186.0)
    assert (bpm.OCTAVE_RANGE_MIN_BPM, bpm.OCTAVE_RANGE_MAX_BPM) == (70.0, 180.0)
    assert analysis_range.bounds(analysis_range.GLOBAL_RANGE) == (70.0, 180.0)

    cleared = client.delete(path)
    assert cleared.status_code == 200
    assert (cleared.json()["range"], cleared.json()["effective"]) == (None, "70-180")
    assert resolve_bpm_range(SID, state_path) == (70.0, 180.0)


def test_an_unknown_range_is_422_and_writes_nothing(client: TestClient, state_path: Path) -> None:
    answer = client.put(f"/api/v1/tracks/{SID}/analysis-range", json={"range": "90-200"})
    assert answer.status_code == 422
    assert answer.json()["detail"]["code"] == "ANALYSIS_RANGE_INVALID"
    assert _stored(state_path) == (None, None)
    missing = client.put(f"/api/v1/tracks/{'0' * 40}/analysis-range", json={"range": "93-186"})
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "TRACK_NOT_FOUND"


def test_get_lists_the_presets_and_the_frontend_matches(client: TestClient) -> None:
    answer = client.get(f"/api/v1/tracks/{SID}/analysis-range")
    assert answer.status_code == 200
    assert answer.json() == {
        "stable_id": SID,
        "range": None,
        "effective": "70-180",
        "choices": list(analysis_range.BPM_RANGES),
    }
    literal = re.search(r"BPM_RANGES = \[([^\]]*)\]", FRONTEND_RANGES.read_text())
    assert literal is not None
    assert tuple(re.findall(r"'([^']+)'", literal.group(1))) == analysis_range.BPM_RANGES
