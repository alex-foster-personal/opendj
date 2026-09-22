"""vocals_demucs_fixture builder contract tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apps.webui.frontend.tests.e2e.support import vocals_demucs_fixture

MDT_LIVE_DEMUCS_ACCEPTANCE = os.environ.get("MDT_LIVE_DEMUCS_ACCEPTANCE") == "1"


def test_vocals_demucs_fixture_refuses_relative_data_dir() -> None:
    code = vocals_demucs_fixture.main(["--data-dir", "relative-vocals-data"])
    assert code == 2


@pytest.mark.skipif(
    not MDT_LIVE_DEMUCS_ACCEPTANCE,
    reason="UNAVAILABLE: set MDT_LIVE_DEMUCS_ACCEPTANCE=1 for demucs fixture build",
)
def test_vocals_demucs_fixture_builds_manifest(tmp_path: Path) -> None:
    data_dir = tmp_path / "vocals-demucs-data"
    manifest = tmp_path / "manifest.json"
    code = vocals_demucs_fixture.main(
        ["--data-dir", str(data_dir), "--manifest", str(manifest)]
    )
    assert code == 0
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["stable_id"]
    assert payload["playlist_id"] == vocals_demucs_fixture.PLAYLIST_ID
    assert payload["demucs_ready"] is True
