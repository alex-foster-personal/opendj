"""``/api/v1/auto-gain`` and the ``auto_gain`` UI pref (AUTOGAIN-01..05).

[if] the route is read [then] it serves the resolved offset and the user value wins, [else stop].

Real SqliteBackend over a real state.db; loudness comes from the own
``loudness`` lane projection, the user value from ``track_fields``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.analysis import store as analysis_store
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

LOUD = "a" * 40
QUIET = "b" * 40
BARE = "c" * 40
STAMP = "2026-10-27T00:00:00+00:00"

pytestmark = pytest.mark.requirement("AUTOGAIN-01")


def _make_state_db(path: Path) -> None:
    conn = state_db.open_rw(path)
    try:
        for sid in (LOUD, QUIET, BARE):
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
                "file_path, created_at, updated_at) VALUES "
                "(?, 'inferred', ?, '[\"Artist\"]', ?, ?, ?)",
                (sid, f"track {sid[0]}", f"/music/{sid[0]}.mp3", STAMP, STAMP),
            )
        conn.commit()
    finally:
        conn.close()
    conn = analysis_store.open_conn(path)
    try:
        for sid, lufs, dbtp in ((LOUD, -6.0, 1.5), (QUIET, -15.0, -2.0)):
            for field, value in (("loudness_lufs", lufs), ("loudness_dbtp", dbtp)):
                conn.execute(
                    "INSERT INTO analysis_projection (stable_id, field, value, status, reason, "
                    "confidence, backend, backend_version, updated_at) "
                    "VALUES (?, ?, ?, 'ok', NULL, 1.0, 'own_loudness.backfill', '1.0.0', ?)",
                    (sid, field, value, STAMP),
                )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    state_path = tmp_path / "state" / "state.db"
    state_path.parent.mkdir(parents=True)
    _make_state_db(state_path)
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_path),
        mount_frontend=False,
    )
    app.state.data_dir = tmp_path
    with TestClient(app) as test_client:
        yield test_client


def _get(client: TestClient, sid: str) -> dict:
    response = client.get(f"/api/v1/auto-gain/tracks/{sid}")
    assert response.status_code == 200, response.text
    return response.json()


def test_default_pref_is_on_at_minus_12_lufs(client: TestClient) -> None:
    """If the auto gain pref is not on at -12 LUFS by default then broken."""
    prefs = client.get("/api/v1/ui-prefs").json()
    assert prefs["auto_gain"] == {"enabled": True, "target_lufs": -12.0}


def test_loud_track_is_cut_and_quiet_track_boost_is_peak_limited(client: TestClient) -> None:
    """If the offset is not target minus measured, or a boost can clip, then broken."""
    loud = _get(client, LOUD)
    assert (loud["status"], loud["effective_offset_db"]) == ("analysed", -6.0)
    quiet = _get(client, QUIET)
    assert quiet["effective_offset_db"] == pytest.approx(1.0)
    assert quiet["limited_by"] == "true_peak"


def test_unanalysed_track_reads_not_analysed_at_zero(client: TestClient) -> None:
    """If a track with no loudness gets a guessed offset then broken."""
    bare = _get(client, BARE)
    assert (bare["status"], bare["effective_offset_db"]) == ("not_analysed", 0.0)
    assert bare["loudness_reason"]


def test_user_offset_wins_survives_and_clears(client: TestClient) -> None:
    """If the user's value loses to analysis, has no provenance, or cannot be cleared then broken."""
    response = client.put(f"/api/v1/auto-gain/tracks/{LOUD}/user-offset", json={"offset_db": -2.5})
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["effective_offset_db"]) == ("user", -2.5)
    got = _get(client, LOUD)
    assert got["effective_offset_db"] == -2.5
    assert got["analysed_offset_db"] == -6.0
    prov = client.get(f"/api/v1/tracks/{LOUD}").json()["provenance"]["auto_gain_user_db"]
    assert (prov["value"], prov["source"]) == (-2.5, "webui")

    cleared = client.put(f"/api/v1/auto-gain/tracks/{LOUD}/user-offset", json={"offset_db": None})
    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["status"], cleared.json()["effective_offset_db"]) == ("analysed", -6.0)


def test_out_of_range_user_offset_is_refused(client: TestClient) -> None:
    """If an out-of-range user offset is stored then broken."""
    response = client.put(f"/api/v1/auto-gain/tracks/{LOUD}/user-offset", json={"offset_db": 40})
    assert response.status_code == 422
    assert _get(client, LOUD)["user_offset_db"] is None


def test_pref_target_and_switch_change_the_offset(client: TestClient) -> None:
    """If the target or the off switch in ui-prefs is ignored then broken."""
    response = client.put("/api/v1/ui-prefs", json={"auto_gain": {"target_lufs": -9.0}})
    assert response.status_code == 200, response.text
    assert response.json()["auto_gain"] == {"enabled": True, "target_lufs": -9.0}
    assert _get(client, LOUD)["effective_offset_db"] == -3.0
    client.put("/api/v1/ui-prefs", json={"auto_gain": {"enabled": False}})
    off = _get(client, LOUD)
    assert (off["status"], off["effective_offset_db"], off["target_lufs"]) == ("off", 0.0, -9.0)


def test_out_of_range_target_is_refused(client: TestClient) -> None:
    """If an impossible target lands in ui-prefs then broken."""
    response = client.put("/api/v1/ui-prefs", json={"auto_gain": {"target_lufs": -40}})
    assert response.status_code == 422


def test_unknown_track_is_404(client: TestClient) -> None:
    """If an unknown track reads as not analysed instead of missing then broken."""
    assert client.get(f"/api/v1/auto-gain/tracks/{'0' * 40}").status_code == 404
