"""Issue #2918: POST /commands master_mute persists to ui-prefs.json."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.opendj_cli.conftest import Engine


@pytest.mark.requirement("UXR-01")
def test_command_master_mute_writes_ui_prefs_to_disk(
    engine: Engine, tmp_path: Path
) -> None:
    """[if] POST /commands carries master_mute [then] ui-prefs.json records it, [else stop]."""
    engine.app.state.data_dir = tmp_path / "data"
    page = engine.page()
    page.start()
    try:
        response = httpx.post(
            f"{engine.base_url}/api/v1/commands",
            json={"single": {"type": "master_mute", "muted": True}},
            timeout=10.0,
        )
        assert response.status_code == 200, response.text
    finally:
        page.stop()

    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["master_muted"] is True
    prefs = httpx.get(f"{engine.base_url}/api/v1/ui-prefs", timeout=5.0)
    assert prefs.status_code == 200
    assert prefs.json()["master_muted"] is True


@pytest.mark.requirement("UXR-01")
def test_command_sequence_master_mute_last_wins_on_disk(
    engine: Engine, tmp_path: Path
) -> None:
    """[if] a sequence ends with master_mute false [then] disk stores false, [else stop]."""
    engine.app.state.data_dir = tmp_path / "data"
    page = engine.page()
    page.start()
    try:
        response = httpx.post(
            f"{engine.base_url}/api/v1/commands",
            json={
                "sequence": [
                    {"type": "master_mute", "muted": True},
                    {"type": "master_mute", "muted": False},
                ]
            },
            timeout=10.0,
        )
        assert response.status_code == 200, response.text
    finally:
        page.stop()

    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["master_muted"] is False
