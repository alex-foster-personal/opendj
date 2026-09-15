"""Payload ``bin/opendj`` launcher coverage for AGENT-05 installed-app parity."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    CLI_LAUNCHER_RELATIVE,
    CLI_LAUNCHER_TEMPLATE,
    LAUNCHER_TEMPLATE,
    MANIFEST_KIND,
    write_launchers,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENDJ_CLI_SRC = REPO_ROOT / "apps" / "opendj_cli"


def _minimal_payload_for_cli_launcher(tmp_path: Path) -> Path:
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "manifest.json").write_text(
        '{"schema":1,"kind":"'
        + MANIFEST_KIND
        + '","identity":{"version":"test","build_id":"test"}}',
        encoding="utf-8",
    )
    (payload / "pylib").mkdir()
    runtime_bin = payload / "runtime" / "bin"
    runtime_bin.mkdir(parents=True)
    runtime_python = runtime_bin / "python3"
    runtime_python.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    runtime_python.chmod(0o755)
    shutil.copytree(OPENDJ_CLI_SRC, payload / "app" / "apps" / "opendj_cli")
    write_launchers(payload)
    return payload


@pytest.mark.requirement("AGENT-05")
def test_bin_opendj_exists_and_is_executable(tmp_path: Path) -> None:
    """[if] a minimal engine payload is built [then] bin/opendj exists and is executable, [else stop]."""
    root = _minimal_payload_for_cli_launcher(tmp_path)
    launcher = root / CLI_LAUNCHER_RELATIVE
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK)


@pytest.mark.requirement("AGENT-05")
def test_cli_launcher_template_matches_engine_env_contract() -> None:
    """[if] the payload launcher templates are rendered [then] they match the engine env contract, [else stop]."""
    for template in (LAUNCHER_TEMPLATE, CLI_LAUNCHER_TEMPLATE):
        assert "unset MDT_REKORDBOX_WRITEBACK_ENABLED" in template
        assert "PYTHONDONTWRITEBYTECODE=1" in template
        assert "PYTHONNOUSERSITE=1" in template
        assert "PYTHONSAFEPATH=1" in template
    assert "-m apps.engine_core serve" in LAUNCHER_TEMPLATE
    assert '-m apps.opendj_cli "$@"' in CLI_LAUNCHER_TEMPLATE


@pytest.mark.requirement("AGENT-05")
def test_bin_opendj_list_verbs_from_clean_env_outside_repo(tmp_path: Path) -> None:
    """[if] bin/opendj runs outside the repo with a clean env [then] --list-verbs succeeds, [else stop]."""
    root = _minimal_payload_for_cli_launcher(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [str(root / CLI_LAUNCHER_RELATIVE), "--list-verbs"],
        cwd=outside,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
