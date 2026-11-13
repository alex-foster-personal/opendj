"""META-06: glossary synonym map must stage into the engine payload.

Issue #2762: the installed app ships ``apps/open_dj/glossary.py`` but not the
``open-dj/synonym-map.json`` data file ``MAP_PATH`` resolves to in the payload
layout, so ``python -m apps.open_dj.glossary dump`` fails with
``FileNotFoundError`` on a tester's Mac.

Issue #13426: dump/lookup must run via ``payload/runtime/bin/python3`` and
``PYTHONPATH`` listing both ``app`` and ``pylib`` (joined with ``os.pathsep``),
not host ``sys.executable`` with ``PYTHONPATH=app`` only.
On Windows the fixture copies the host interpreter to ``runtime/bin/python3.exe``
instead of a POSIX ``#!/bin/sh`` shim.

[if] app source is staged [then] the synonym map ships and glossary dump/lookup run, [else stop].

Regression one-liners:
  - if ``stage_app_source`` omits ``open-dj/synonym-map.json`` then the
    installed glossary CLI is dead -> broken
  - if the map is absent from the repo at build time then the build must fail
    loudly, not at runtime on a tester -> broken
  - if ``load_synonym_map()`` cannot run from the staged layout then verify
    must catch it -> broken
  - if dump uses host ``sys.executable`` without ``runtime/bin/python3`` then
    installed-app parity is untested -> broken
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    GLOSSARY_MAP_PAYLOAD_RELATIVE,
    GLOSSARY_MAP_REPO_RELATIVE,
    MANIFEST_KIND,
    PayloadBuildError,
    _stage_glossary_synonym_map,
    stage_app_source,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: Denies the payload run the repo checkout; see ``test_engine_payload_opendj_cli``.
REPO_ISOLATION_GUARD: str = '''\
"""Undo the test venv's editable install of this repo inside the payload run."""

import sys
from pathlib import Path

REPO_ROOT = {repo!r}

sys.meta_path[:] = [
    finder
    for finder in sys.meta_path
    if not getattr(finder, "__module__", "").startswith("__editable__")
]
sys.path[:] = [
    entry for entry in sys.path if Path(entry).resolve() != Path(REPO_ROOT)
]
'''

pytestmark = pytest.mark.requirement("META-06")


def _minimal_build_dir(tmp_path: Path) -> Path:
    build_dir = tmp_path / "frontend-build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    return build_dir


def _bundled_runtime_python(payload: Path) -> Path:
    """Payload-local interpreter argv for glossary subprocess smoke tests."""
    if sys.platform == "win32":
        return payload / "runtime" / "bin" / "python3.exe"
    return payload / "runtime" / "bin" / "python3"


def _minimal_staged_payload_for_glossary(tmp_path: Path) -> Path:
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "manifest.json").write_text(
        '{"schema":1,"kind":"' + MANIFEST_KIND + '","identity":{"version":"test","build_id":"test"}}',
        encoding="utf-8",
    )
    (payload / "pylib").mkdir()
    (payload / "pylib" / "sitecustomize.py").write_text(
        REPO_ISOLATION_GUARD.format(repo=str(REPO_ROOT)),
        encoding="utf-8",
    )
    runtime_bin = payload / "runtime" / "bin"
    runtime_bin.mkdir(parents=True)
    if sys.platform == "win32":
        shutil.copy2(sys.executable, runtime_bin / "python3.exe")
    else:
        runtime_python = runtime_bin / "python3"
        runtime_python.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        runtime_python.chmod(0o755)
    stage_app_source(REPO_ROOT, payload / "app", _minimal_build_dir(tmp_path))
    return payload


def _run_glossary_from_payload(
    payload: Path,
    tmp_path: Path,
    *cli_args: str,
) -> subprocess.CompletedProcess[str]:
    """Run ``apps.open_dj.glossary`` via bundled runtime outside the repo."""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    app_root = payload / "app"
    pylib_root = payload / "pylib"
    return subprocess.run(
        [
            str(_bundled_runtime_python(payload)),
            "-m",
            "apps.open_dj.glossary",
            *cli_args,
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "PYTHONPATH": os.pathsep.join([str(app_root), str(pylib_root)]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        },
    )


@pytest.mark.requirement("META-06")
def test_stage_app_source_copies_glossary_synonym_map(tmp_path: Path) -> None:
    """[if] app source is staged [then] synonym map bytes match the repo, [else stop]."""
    destination = tmp_path / "payload" / "app"
    stage_app_source(REPO_ROOT, destination, _minimal_build_dir(tmp_path))
    staged_map = destination / GLOSSARY_MAP_PAYLOAD_RELATIVE
    assert staged_map.is_file()
    repo_map = REPO_ROOT / GLOSSARY_MAP_REPO_RELATIVE
    assert staged_map.read_bytes() == repo_map.read_bytes()


@pytest.mark.requirement("META-06")
def test_stage_glossary_synonym_map_fails_when_repo_map_missing(tmp_path: Path) -> None:
    """[if] repo map is absent [then] staging raises PayloadBuildError, [else stop]."""
    destination = tmp_path / "app"
    fake_root = tmp_path / "empty-repo"
    fake_root.mkdir()
    with pytest.raises(PayloadBuildError, match="glossary synonym map missing"):
        _stage_glossary_synonym_map(fake_root, destination)


@pytest.mark.requirement("META-06")
def test_glossary_dump_succeeds_from_staged_payload_layout(tmp_path: Path) -> None:
    """[if] staged payload has map [then] bundled-runtime dump succeeds, [else stop]."""
    payload = _minimal_staged_payload_for_glossary(tmp_path)
    result = _run_glossary_from_payload(payload, tmp_path, "dump")
    assert result.returncode == 0, result.stderr or result.stdout
    body = json.loads(result.stdout)
    assert "entries" in body
    assert body["entries"]


@pytest.mark.requirement("META-06")
def test_glossary_lookup_succeeds_from_staged_payload_layout(tmp_path: Path) -> None:
    """[if] staged payload has map [then] bundled-runtime lookup succeeds, [else stop]."""
    payload = _minimal_staged_payload_for_glossary(tmp_path)
    result = _run_glossary_from_payload(payload, tmp_path, "lookup", "tbpm")
    assert result.returncode == 0, result.stderr or result.stdout
    entry = json.loads(result.stdout)
    assert entry["canonical"]
