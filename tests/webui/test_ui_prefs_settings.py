"""ui-prefs extensions + settings AI routes (settings panel v1)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend


@pytest.fixture
def prefs_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_db.open_rw(state_path).close()
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
        state_db_path=str(state_path),
    )
    assert isinstance(app.state.backend, SqliteBackend), (
        "ui-prefs requests must exercise the production SQLite backend composition"
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        yield client


def test_ui_prefs_defaults_include_sync_and_hide_todo(prefs_client: TestClient) -> None:
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 200
    body = r.json()
    assert body["theme"] == "dark"
    assert body["hide_todo_settings"] is False
    assert body["auto_sync"] == {
        "rekordbox": False,
        "djay": False,
        "open_dj": False,
    }
    assert body["technically_working_animate"] is True
    assert body["show_agent_pins"] is True


def test_ui_prefs_show_agent_pins_round_trips(prefs_client: TestClient, tmp_path: Path) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_agent_pins": False})
    assert r.status_code == 200
    assert r.json()["show_agent_pins"] is False
    assert (
        json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())["show_agent_pins"]
        is False
    )
    assert prefs_client.get("/api/v1/ui-prefs").json()["show_agent_pins"] is False


def test_ui_prefs_put_persists_technically_working_animate(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    r = prefs_client.put(
        "/api/v1/ui-prefs",
        json={"technically_working_animate": False},
    )
    assert r.status_code == 200
    assert r.json()["technically_working_animate"] is False

    disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert disk["technically_working_animate"] is False

    # Round-trips on a fresh GET too, not just the PUT response echo.
    r2 = prefs_client.get("/api/v1/ui-prefs")
    assert r2.json()["technically_working_animate"] is False


def test_ui_prefs_rejects_non_boolean_animate(prefs_client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "data" / "state" / "ui-prefs.json").write_text(
        json.dumps({"technically_working_animate": "yes"})
    )
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UI_PREFS_INVALID"


@pytest.mark.requirement("DECKUX-02")
def test_ui_prefs_jog_radial_waveform_defaults_false(prefs_client: TestClient) -> None:
    """[if] ui-prefs are fetched with no stored value [then] jog_radial_waveform is false, [else stop]."""
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 200
    assert r.json()["jog_radial_waveform"] is False


@pytest.mark.requirement("DECKUX-02")
def test_ui_prefs_put_persists_jog_radial_waveform(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] ui-prefs puts jog_radial_waveform [then] GET and disk persist the value, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"jog_radial_waveform": True})
    assert r.status_code == 200
    assert r.json()["jog_radial_waveform"] is True

    disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert disk["jog_radial_waveform"] is True

    r2 = prefs_client.get("/api/v1/ui-prefs")
    assert r2.json()["jog_radial_waveform"] is True


@pytest.mark.requirement("DECKUX-02")
def test_ui_prefs_rejects_non_boolean_jog_radial_waveform(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] jog_radial_waveform is not a boolean [then] GET returns 422 UI_PREFS_INVALID, [else stop]."""
    (tmp_path / "data" / "state" / "ui-prefs.json").write_text(
        json.dumps({"jog_radial_waveform": "yes"})
    )
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UI_PREFS_INVALID"


def test_ui_prefs_put_merges_sync_destinations(prefs_client: TestClient, tmp_path: Path) -> None:
    r = prefs_client.put(
        "/api/v1/ui-prefs",
        json={
            "hide_todo_settings": True,
            "auto_sync": {"rekordbox": True, "djay": False, "open_dj": True},
            "theme": "light",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["hide_todo_settings"] is True
    assert body["auto_sync"]["rekordbox"] is True
    assert body["auto_sync"]["open_dj"] is True
    assert body["theme"] == "light"

    disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert disk["auto_sync"]["rekordbox"] is True


def test_settings_dump_still_intact(prefs_client: TestClient) -> None:
    r = prefs_client.get("/api/v1/settings")
    assert r.status_code == 200
    assert any(g["group"] == "Network" for g in r.json()["groups"])


def test_ai_search_fails_loud_without_key(
    prefs_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    r = prefs_client.post(
        "/api/v1/settings/ai-search",
        json={"query": "dark mode", "catalog_ids": ["theme"]},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "XAI_API_KEY_MISSING"


def _fake_xai_response(payload: dict[str, Any]) -> Any:
    content = json.dumps(payload)

    class _Resp(io.BytesIO):
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _urlopen(req: object, timeout: float = 0) -> _Resp:
        envelope = {
            "model": "mock-grok",
            "choices": [{"message": {"content": content}}],
        }
        return _Resp(json.dumps(envelope).encode("utf-8"))

    return _urlopen


def test_ai_search_filters_to_catalog(
    prefs_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(
        "apps.webui.server.settings_xai.urllib.request.urlopen",
        _fake_xai_response({"ids": ["theme", "theme", "nope", "beat_sync_max"]}),
    )
    r = prefs_client.post(
        "/api/v1/settings/ai-search",
        json={
            "query": "make it dark",
            "catalog_ids": ["theme", "beat_sync_max", "hide_broken_links"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ids"] == ["theme", "beat_sync_max"]
    assert body["model"] == "mock-grok"


def test_ai_apply_allowlist_and_refuse(
    prefs_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(
        "apps.webui.server.settings_xai.urllib.request.urlopen",
        _fake_xai_response(
            {
                "key": "theme",
                "value": "dark",
                "rationale": "user asked for dark mode",
            }
        ),
    )
    r = prefs_client.post(
        "/api/v1/settings/ai-apply",
        json={"instruction": "turn on dark mode"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["proposal"]["key"] == "theme"
    assert body["proposal"]["value"] == "dark"

    monkeypatch.setattr(
        "apps.webui.server.settings_xai.urllib.request.urlopen",
        _fake_xai_response({"key": "rb.quantize", "value": True, "rationale": "nope"}),
    )
    r2 = prefs_client.post(
        "/api/v1/settings/ai-apply",
        json={"instruction": "enable quantize"},
    )
    assert r2.status_code == 200
    assert r2.json()["ok"] is False
    assert "disallowed" in (r2.json()["error"] or "")


# ----- eval set (JSONL-driven) ---------------------------------------------

_EVAL_PATH = Path(__file__).with_name("settings_ai_apply_evals.jsonl")


def _load_evals() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_line in _EVAL_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


@pytest.mark.parametrize("case", _load_evals(), ids=lambda c: c["id"])
def test_ai_apply_eval_cases(
    prefs_client: TestClient, monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(
        "apps.webui.server.settings_xai.urllib.request.urlopen",
        _fake_xai_response(case["model_json"]),
    )
    r = prefs_client.post(
        "/api/v1/settings/ai-apply",
        json={"instruction": case["instruction"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is case["expect_ok"]
    if case["expect_ok"]:
        assert body["proposal"]["key"] == case["expect_key"]
        assert body["proposal"]["value"] == case["expect_value"]
    else:
        assert body["proposal"] is None


def test_ai_apply_live_optional(prefs_client: TestClient) -> None:
    """Optional live call - skipped unless RUN_LIVE_XAI=1 and key present."""
    import os

    if os.environ.get("RUN_LIVE_XAI") != "1":
        pytest.skip("set RUN_LIVE_XAI=1 to exercise live xAI")
    if not (os.environ.get("XAI_API_KEY") or "").strip():
        pytest.skip("XAI_API_KEY not set")
    r = prefs_client.post(
        "/api/v1/settings/ai-apply",
        json={"instruction": "turn on dark mode"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["proposal"]["key"] == "theme"
    assert body["proposal"]["value"] in ("dark", "light")


pytestmark = pytest.mark.rb_parity


# --- level calibration ------------------------------------------------------
#
# The meter shipped with a fixed red band at -3 dBFS, calibrated for a mixing
# desk where the operator rides trim. These tests pin the by-ear calibration
# contract without asserting a result from a private listening corpus.
#
# Regression lines:
# - if defaults stop being "unset and disabled" then an install with no
#   calibration silently changes behaviour under the user
# - if an out-of-range level is accepted then a typo can put red past any
#   reachable level and the meter never lights again
# - if enabling a toggle without a captured level is accepted then the user
#   believes a calibration is applied when nothing was ever measured


def test_level_calibration_defaults_to_unset_and_disabled(prefs_client: TestClient) -> None:
    cal = prefs_client.get("/api/v1/ui-prefs").json()["level_calibration"]
    assert cal == {
        "red_dbfs": None,
        "red_enabled": False,
        "ceiling_dbfs": None,
        "ceiling_enabled": False,
    }


def test_level_calibration_round_trips(prefs_client: TestClient) -> None:
    body = {
        "level_calibration": {
            "red_dbfs": -6.5,
            "red_enabled": True,
            "ceiling_dbfs": -1.0,
            "ceiling_enabled": True,
        }
    }
    assert prefs_client.put("/api/v1/ui-prefs", json=body).status_code == 200
    cal = prefs_client.get("/api/v1/ui-prefs").json()["level_calibration"]
    assert cal["red_dbfs"] == -6.5
    assert cal["red_enabled"] is True
    assert cal["ceiling_dbfs"] == -1.0
    assert cal["ceiling_enabled"] is True


def test_r_and_m_are_independent(prefs_client: TestClient) -> None:
    body = {"level_calibration": {"red_dbfs": -8.0, "red_enabled": True}}
    assert prefs_client.put("/api/v1/ui-prefs", json=body).status_code == 200
    cal = prefs_client.get("/api/v1/ui-prefs").json()["level_calibration"]
    assert cal["red_enabled"] is True
    assert cal["ceiling_enabled"] is False, "enabling R must not enable M"
    assert cal["ceiling_dbfs"] is None


@pytest.mark.parametrize("bad", [-120.0, 40.0])
def test_out_of_range_level_is_refused(prefs_client: TestClient, bad: float) -> None:
    body = {"level_calibration": {"red_dbfs": bad}}
    r = prefs_client.put("/api/v1/ui-prefs", json=body)
    assert r.status_code == 422
    assert "dBFS" in json.dumps(r.json())


def test_a_ceiling_above_0_dbfs_is_refused(prefs_client: TestClient) -> None:
    # min(1, 10**(dbfs/20)) is a no-op attenuation for any dbfs above 0, so
    # accepting one here would let M read as enabled while the master gain
    # stays untouched -- the same "toggle lit, nothing changed" defect the
    # enabled-requires-a-level check above exists to prevent.
    r = prefs_client.put(
        "/api/v1/ui-prefs",
        json={"level_calibration": {"ceiling_dbfs": 2.0, "ceiling_enabled": True}},
    )
    assert r.status_code == 422
    assert "ceiling_dbfs" in json.dumps(r.json())
    # Red keeps the wider range: a meter anchor above 0 dBFS is a real anchor,
    # not a no-op, so the same value is fine there.
    r = prefs_client.put(
        "/api/v1/ui-prefs",
        json={"level_calibration": {"red_dbfs": 2.0, "red_enabled": True}},
    )
    assert r.status_code == 200


def test_enabling_without_a_captured_level_is_refused(prefs_client: TestClient) -> None:
    # Silently accepting this would show the toggle lit while nothing changed.
    r = prefs_client.put(
        "/api/v1/ui-prefs", json={"level_calibration": {"red_enabled": True}}
    )
    assert r.status_code == 422
    assert "requires red_dbfs" in json.dumps(r.json())


def test_calibration_survives_an_unrelated_pref_write(prefs_client: TestClient) -> None:
    prefs_client.put(
        "/api/v1/ui-prefs",
        json={"level_calibration": {"ceiling_dbfs": -2.5, "ceiling_enabled": True}},
    )
    prefs_client.put("/api/v1/ui-prefs", json={"hide_todo_settings": True})
    cal = prefs_client.get("/api/v1/ui-prefs").json()["level_calibration"]
    assert cal["ceiling_dbfs"] == -2.5, "an unrelated write dropped the calibration"
    assert cal["ceiling_enabled"] is True


def test_capturing_r_does_not_wipe_an_already_captured_m(prefs_client: TestClient) -> None:
    # A PUT naming only ceiling_* must not silently reset red_* to the
    # LevelCalibrationOut per-field defaults, and vice versa: an agent driving
    # the API directly (rather than the frontend, which happens to always
    # round-trip all four fields together) would otherwise corrupt the half
    # it never mentioned. Regression for a bug caught live while verifying
    # #1475's agent-native parity requirement.
    prefs_client.put(
        "/api/v1/ui-prefs",
        json={"level_calibration": {"red_dbfs": -6.5, "red_enabled": True}},
    )
    prefs_client.put(
        "/api/v1/ui-prefs",
        json={"level_calibration": {"ceiling_dbfs": -1.0, "ceiling_enabled": True}},
    )
    cal = prefs_client.get("/api/v1/ui-prefs").json()["level_calibration"]
    assert cal["red_dbfs"] == -6.5, "capturing M wiped R's already-captured level"
    assert cal["red_enabled"] is True
    assert cal["ceiling_dbfs"] == -1.0
    assert cal["ceiling_enabled"] is True


# --- atomic persistence (issue #1034, PARITY-13) -----------------------------


def _fire_together(calls: list) -> list:
    barrier = threading.Barrier(len(calls))

    def _run(fn):
        barrier.wait()
        return fn()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(_run, fn) for fn in calls]
        return [f.result() for f in futures]


@pytest.mark.requirement("PARITY-13")
def test_ui_prefs_replace_failure_preserves_prior_document(
    prefs_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] replace fails after temp write [then] prior ui-prefs bytes stay intact, [else stop]."""
    prefs_path = tmp_path / "data" / "state" / "ui-prefs.json"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    seeded = {"theme": "dark", "hide_todo_settings": True}
    prefs_path.write_text(json.dumps(seeded, indent=2) + "\n", encoding="utf-8")
    before = prefs_path.read_bytes()
    real_replace = os.replace

    def failing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if str(src).endswith(".tmp"):
            raise OSError("simulated crash before rename")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    with TestClient(prefs_client.app, raise_server_exceptions=False) as client:
        response = client.put("/api/v1/ui-prefs", json={"theme": "light"})
    assert response.status_code == 500
    assert prefs_path.read_bytes() == before
    assert list(prefs_path.parent.glob(".ui-prefs.json.*.tmp")) == []


@pytest.mark.requirement("PARITY-13")
def test_ui_prefs_concurrent_puts_merge_both_updates(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] two PUTs race [then] disk holds one merged document with both keys, [else stop]."""
    prefs_path = tmp_path / "data" / "state" / "ui-prefs.json"

    def _put_theme_light():
        return prefs_client.put("/api/v1/ui-prefs", json={"theme": "light"})

    def _put_hide_todo():
        return prefs_client.put("/api/v1/ui-prefs", json={"hide_todo_settings": True})

    responses = _fire_together([_put_theme_light, _put_hide_todo])
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    disk = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert disk["theme"] == "light"
    assert disk["hide_todo_settings"] is True
    assert prefs_client.get("/api/v1/ui-prefs").status_code == 200


@pytest.mark.requirement("PARITY-13")
def test_ui_prefs_put_uses_mdt_data_dir(tmp_path: Path) -> None:
    """[if] MDT_DATA_DIR selects data root [then] ui-prefs.json lands under it, [else stop]."""
    mdt_root = tmp_path / "mdt-data"
    repo = Path(__file__).resolve().parents[2]
    code = f"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

mdt_root = Path({str(mdt_root)!r})
state_path = mdt_root / "state" / "state.db"
state_db.open_rw(state_path).close()
app = create_app(
    backend=SqliteBackend(state_path),
    bind_host="127.0.0.1",
    hostname="test-host",
    mount_frontend=False,
    state_db_path=str(state_path),
)
with TestClient(app, base_url="http://test-host") as client:
    response = client.put("/api/v1/ui-prefs", json={{"theme": "light"}})
    assert response.status_code == 200, response.text
prefs_path = mdt_root / "state" / "ui-prefs.json"
assert prefs_path.is_file()
assert json.loads(prefs_path.read_text(encoding="utf-8"))["theme"] == "light"
"""
    env = {**os.environ, "MDT_DATA_DIR": str(mdt_root), "PYTHONPATH": str(repo)}
    subprocess.run([sys.executable, "-c", code], env=env, check=True, cwd=repo)
