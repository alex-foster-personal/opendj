"""META-06: glossary synonym map must stage into the engine payload.

Issue #2762: the installed app ships ``apps/open_dj/glossary.py`` but not the
``open-dj/synonym-map.json`` data file ``MAP_PATH`` resolves to in the payload
layout, so ``python -m apps.open_dj.glossary dump`` fails with
``FileNotFoundError`` on a tester's Mac.

[if] app source is staged [then] the synonym map ships and glossary dump/lookup run, [else stop].

Regression one-liners:
  - if ``stage_app_source`` omits ``open-dj/synonym-map.json`` then the
    installed glossary CLI is dead -> broken
  - if the map is absent from the repo at build time then the build must fail
    loudly, not at runtime on a tester -> broken
  - if ``load_synonym_map()`` cannot run from the staged layout then verify
    must catch it -> broken
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    GLOSSARY_MAP_PAYLOAD_RELATIVE,
    GLOSSARY_MAP_REPO_RELATIVE,
    PayloadBuildError,
    _stage_glossary_synonym_map,
    stage_app_source,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.requirement("META-06")


def _minimal_build_dir(tmp_path: Path) -> Path:
    build_dir = tmp_path / "frontend-build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    return build_dir


@pytest.mark.requirement("META-06")
def test_stage_app_source_copies_glossary_synonym_map(tmp_path: Path) -> None:
    destination = tmp_path / "payload" / "app"
    stage_app_source(REPO_ROOT, destination, _minimal_build_dir(tmp_path))
    staged_map = destination / GLOSSARY_MAP_PAYLOAD_RELATIVE
    assert staged_map.is_file()
    repo_map = REPO_ROOT / GLOSSARY_MAP_REPO_RELATIVE
    assert staged_map.read_bytes() == repo_map.read_bytes()


@pytest.mark.requirement("META-06")
def test_stage_glossary_synonym_map_fails_when_repo_map_missing(tmp_path: Path) -> None:
    destination = tmp_path / "app"
    fake_root = tmp_path / "empty-repo"
    fake_root.mkdir()
    with pytest.raises(PayloadBuildError, match="glossary synonym map missing"):
        _stage_glossary_synonym_map(fake_root, destination)


@pytest.mark.requirement("META-06")
def test_glossary_dump_succeeds_from_staged_payload_layout(tmp_path: Path) -> None:
    destination = tmp_path / "payload" / "app"
    stage_app_source(REPO_ROOT, destination, _minimal_build_dir(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "apps.open_dj.glossary", "dump"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PYTHONPATH": str(destination),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        },
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert "entries" in payload
    assert payload["entries"]


@pytest.mark.requirement("META-06")
def test_glossary_lookup_succeeds_from_staged_payload_layout(tmp_path: Path) -> None:
    destination = tmp_path / "payload" / "app"
    stage_app_source(REPO_ROOT, destination, _minimal_build_dir(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "apps.open_dj.glossary", "lookup", "tbpm"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PYTHONPATH": str(destination),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        },
    )
    assert result.returncode == 0, result.stderr or result.stdout
    entry = json.loads(result.stdout)
    assert entry["canonical"]
