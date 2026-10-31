"""The live-engine acceptance pins its audio before it copies any of it.

The engine test itself needs ffmpeg and the canonical decoder. This check does
not: it reads the committed manifest and the current fixture bytes.

[if] a pinned fixture byte changes [then] verification fails before copy, [else stop].
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.webui.server import ahead_analysis as aa
from tests.health_lights.test_ahead_analysis_live_engine import (
    FIXTURE_MANIFEST,
    _verified_fixture_audio,
)


def test_pinned_fixture_audio_matches_the_manifest() -> None:
    """[if] a pinned fixture byte changes [then] verification fails, [else stop]."""
    paths = _verified_fixture_audio()
    assert len(paths) > aa.LANE_CHUNK
    assert all(path.is_file() for path in paths)


def test_a_wrong_checksum_fails_before_any_copy(tmp_path: Path) -> None:
    """[if] the manifest digest is not the file [then] verification fails, [else stop]."""
    manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    _rel, pinned = next(iter(manifest["files"].items()))
    pinned["sha256"] = "sha256:" + "0" * 64
    bad = tmp_path / "manifest.json"
    bad.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="does not match its manifest checksum"):
        _verified_fixture_audio(bad)


def test_an_unknown_manifest_version_fails_closed(tmp_path: Path) -> None:
    """[if] the manifest version is not 1 [then] verification fails closed, [else stop]."""
    manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    manifest["contract_version"] = 99
    bad = tmp_path / "manifest.json"
    bad.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="Unsupported ahead-analysis fixture manifest"):
        _verified_fixture_audio(bad)
