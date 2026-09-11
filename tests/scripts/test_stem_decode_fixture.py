"""stem_decode_fixture builder contract tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from apps.webui.frontend.tests.e2e.support import stem_decode_fixture


def test_stem_decode_fixture_refuses_relative_data_dir() -> None:
    relative = Path("relative-stem-decode-data")
    code = stem_decode_fixture.main(["--data-dir", str(relative)])
    assert code == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required for FLAC parts")
def test_stem_decode_fixture_builds_bundle_and_manifest(tmp_path: Path) -> None:
    data_dir = tmp_path / "stem-decode-data"
    manifest = tmp_path / "manifest.json"
    code = stem_decode_fixture.main(["--data-dir", str(data_dir), "--manifest", str(manifest)])
    assert code == 0
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["stable_id"]
    assert payload["layout"] == "demucs4"
    bundle = data_dir / "state" / "stems" / payload["stable_id"] / "manifest.json"
    assert bundle.is_file()


def test_stem_decode_fixture_unknown_without_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stem_decode_fixture.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc:
        stem_decode_fixture.build(tmp_path / "data")
    assert exc.value.code == 3
