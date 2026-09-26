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
APPS_SRC = REPO_ROOT / "apps"

#: The ``apps`` packages the CLI reaches while starting up. A real payload
#: carries the whole tree (``build_engine_payload.APP_SOURCE_ROOTS``), so this
#: is the reachable slice of it, not a different contract: the CLI package,
#: the engine package ``apps.opendj_cli.origin`` re-exports through (issue
#: #2942), and ``shared``, which has owned the lock-file reader itself
#: (``apps.shared.engine_origin``) since PR #3831.
CLI_PAYLOAD_PACKAGES: tuple[str, ...] = ("opendj_cli", "engine_core", "shared")

#: Denies the payload run the repo checkout, the way a real runtime does.
#:
#: The fixture's ``runtime/bin/python3`` is a shim over the test venv, and that
#: venv carries this repo's editable install: a meta path finder mapping every
#: ``apps.*`` name back to the checkout, or (in the other setuptools layout)
#: the checkout on ``sys.path``. Either one answers for a package the payload
#: does not ship, so the launcher boots on repo code and the test passes on a
#: payload that cannot start anywhere else -- which is how a payload missing
#: ``apps/engine_core`` stayed green on developer machines and failed in CI.
#: A payload runtime has no editable install, so this restores the real
#: property rather than inventing a stricter one. ``pylib`` is on the
#: launcher's PYTHONPATH, so ``site`` runs this before the CLI imports
#: anything. Third-party wheels still come from the venv, standing in for the
#: ``pylib`` a built payload ships.
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
    (payload / "pylib" / "sitecustomize.py").write_text(
        REPO_ISOLATION_GUARD.format(repo=str(REPO_ROOT)),
        encoding="utf-8",
    )
    runtime_bin = payload / "runtime" / "bin"
    runtime_bin.mkdir(parents=True)
    runtime_python = runtime_bin / "python3"
    runtime_python.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    runtime_python.chmod(0o755)
    staged_apps = payload / "app" / "apps"
    staged_apps.mkdir(parents=True)
    shutil.copy2(APPS_SRC / "__init__.py", staged_apps / "__init__.py")
    for package in CLI_PAYLOAD_PACKAGES:
        shutil.copytree(
            APPS_SRC / package,
            staged_apps / package,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    write_launchers(payload)
    return payload


def _run_cli_launcher(
    payload: Path, tmp_path: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run ``bin/opendj`` outside the repo with an env that carries nothing."""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(
        [str(payload / CLI_LAUNCHER_RELATIVE), *args],
        cwd=outside,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )


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
    result = _run_cli_launcher(root, tmp_path, "--list-verbs")
    assert result.returncode == 0, result.stderr or result.stdout
    assert "load" in result.stdout


@pytest.mark.requirement("AGENT-05")
def test_bin_opendj_cannot_borrow_a_missing_package_from_the_repo(
    tmp_path: Path,
) -> None:
    """[if] the payload lacks a CLI package [then] bin/opendj fails naming it, [else stop].

    The negative control for the test above: with the repo reachable, a
    payload missing ``apps/engine_core`` still started and the passing run
    proved nothing about the payload. This one deletes a staged package and
    requires the failure, so a green ``--list-verbs`` is evidence that the
    payload itself carries what the CLI imports.
    """
    root = _minimal_payload_for_cli_launcher(tmp_path)
    shutil.rmtree(root / "app" / "apps" / "engine_core")
    result = _run_cli_launcher(root, tmp_path, "--list-verbs")
    assert result.returncode != 0
    assert "No module named 'apps.engine_core'" in result.stderr
