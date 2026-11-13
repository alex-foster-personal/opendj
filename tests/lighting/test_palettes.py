"""Palette variants behind ``lighting.palette`` (issue #10103)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.feature_flags import load_flags
from apps.lighting.contract import LIGHTING_FLAG_ID
from apps.lighting.http import bind_lighting_runtime, router
from apps.lighting.runtime import FeedFrame, LightingRuntime
from apps.lighting.show_map import PALETTES_DIR, ShowMapError, load_palette, palette_entropy

SIX_SECTION_PLAN = ["wash_dim", "sweep_slow", "wash_full", "pulse_white", "wash_dim", "split_two_tone"]


def _client(tmp_path: Path) -> tuple[TestClient, LightingRuntime, Path]:
    flags = tmp_path / "flags"
    flags.mkdir()
    (flags / "feature-flags.json").write_text(json.dumps({LIGHTING_FLAG_ID: True}))
    log = tmp_path / "state" / "lighting" / "variant_picks.ndjson"
    runtime = LightingRuntime(pick_log=log)
    app = FastAPI()
    app.state.feature_flags = load_flags(flags)
    bind_lighting_runtime(app, runtime)
    app.include_router(router, prefix="/api/v1")
    return TestClient(app), runtime, log


def test_palette_file_id_mismatch_names_file_and_id(tmp_path: Path) -> None:
    """[if] a palette file misses a look id or names an unknown one [then] loading fails naming file and id, [else stop]."""
    data = json.loads((PALETTES_DIR / "cool.json").read_text())
    del data["looks"]["wash_dim"]
    data["looks"]["ghost_look"] = ["#ffffff"]
    (tmp_path / "bad.json").write_text(json.dumps(data))
    with pytest.raises(ShowMapError) as info:
        load_palette("bad", palettes_dir=tmp_path)
    text = str(info.value)
    assert "bad.json" in text and "wash_dim" in text and "ghost_look" in text


def test_set_palette_reports_status_and_switches_on_downbeat(tmp_path: Path) -> None:
    """[if] palette is set to warm [then] status reports warm and frames switch only on the next downbeat, [else stop]."""
    client, runtime, _log = _client(tmp_path)
    assert client.get("/api/v1/lighting/status").json()["palette"] == "cool"
    response = client.put("/api/v1/lighting/settings/palette", json={"value": "warm"})
    assert response.status_code == 200
    assert response.json()["previous"] == "cool"
    assert client.get("/api/v1/lighting/status").json()["palette"] == "warm"
    assert runtime.palette.name == "cool"
    real_ingest = runtime.ingest
    runtime.ingest = lambda frame: [{"type": "beat", "is_downbeat": False}]  # type: ignore[method-assign]
    runtime.process(FeedFrame(decks=(), weights={}, host_time_ns=1))
    assert runtime.palette.name == "cool"
    runtime.ingest = lambda frame: [{"type": "beat", "is_downbeat": True}]  # type: ignore[method-assign]
    runtime.process(FeedFrame(decks=(), weights={}, host_time_ns=2))
    runtime.ingest = real_ingest  # type: ignore[method-assign]
    assert runtime.palette.name == "warm"


def test_value_outside_allowed_answers_400_and_keeps_setting(tmp_path: Path) -> None:
    """[if] a value outside the four is set [then] the route answers 400 naming allowed values, setting unchanged, [else stop]."""
    client, runtime, log = _client(tmp_path)
    response = client.put("/api/v1/lighting/settings/palette", json={"value": "neon"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["allowed"] == ["cool", "warm", "vivid", "contrast"]
    assert runtime.settings.get("palette") == "cool"
    assert not log.exists()


def test_palette_change_appends_pick_log_line(tmp_path: Path) -> None:
    """[if] a palette changes [then] variant_picks.ndjson gains key, from, to, at, decks_playing, [else stop]."""
    client, _runtime, log = _client(tmp_path)
    client.put("/api/v1/lighting/settings/palette", json={"value": "vivid"})
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(lines) == 1
    assert set(lines[0]) == {"key", "from", "to", "at", "decks_playing"}
    assert (lines[0]["key"], lines[0]["from"], lines[0]["to"]) == ("lighting.palette", "cool", "vivid")


def test_palette_entropy_ratchet() -> None:
    """[if] the four palettes are measured over a 6-section plan [then] entropy orders contrast >= vivid > warm, cool, [else stop]."""
    scores = {
        name: palette_entropy(load_palette(name), SIX_SECTION_PLAN) for name in ("cool", "warm", "vivid", "contrast")
    }
    assert scores == {"cool": 2, "warm": 2, "vivid": 4, "contrast": 7}
    assert scores["contrast"] >= scores["vivid"] > max(scores["warm"], scores["cool"])
