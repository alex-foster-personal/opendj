"""vocals_demucs_fixture builder contract tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apps.webui.frontend.tests.e2e.support import vocals_demucs_fixture

MDT_LIVE_DEMUCS_ACCEPTANCE = os.environ.get("MDT_LIVE_DEMUCS_ACCEPTANCE") == "1"


SOURCE_AUDIO = os.environ.get("VOCALS_DEMUCS_OVERLAY_SOURCE_AUDIO")
CLIP_START_S = os.environ.get("VOCALS_DEMUCS_OVERLAY_CLIP_START_S")


def test_vocals_demucs_fixture_refuses_relative_data_dir() -> None:
    code = vocals_demucs_fixture.main(
        [
            "--data-dir", "relative-vocals-data",
            "--source-audio", "/abs/vocal.mp3",
            "--clip-start-s", "0",
        ]
    )
    assert code == 2


def test_vocals_demucs_fixture_refuses_missing_source_audio(tmp_path: Path) -> None:
    """[if] no real vocal audio is given [then] the builder refuses (rc 2)."""
    code = vocals_demucs_fixture.main(["--data-dir", str(tmp_path / "data")])
    assert code == 2
    assert not (tmp_path / "data").exists()


@pytest.mark.skipif(
    not (MDT_LIVE_DEMUCS_ACCEPTANCE and SOURCE_AUDIO and CLIP_START_S),
    reason=(
        "UNAVAILABLE: set MDT_LIVE_DEMUCS_ACCEPTANCE=1, "
        "VOCALS_DEMUCS_OVERLAY_SOURCE_AUDIO and VOCALS_DEMUCS_OVERLAY_CLIP_START_S "
        "for demucs fixture build"
    ),
)
def test_vocals_demucs_fixture_builds_manifest(tmp_path: Path) -> None:
    data_dir = tmp_path / "vocals-demucs-data"
    manifest = tmp_path / "manifest.json"
    code = vocals_demucs_fixture.main(
        [
            "--data-dir", str(data_dir),
            "--manifest", str(manifest),
            "--source-audio", str(SOURCE_AUDIO),
            "--clip-start-s", str(CLIP_START_S),
        ]
    )
    assert code == 0
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["stable_id"]
    assert payload["playlist_id"] == vocals_demucs_fixture.PLAYLIST_ID
    assert payload["demucs_ready"] is True
