"""INSTALL-22: staged *.pth imports must resolve; startup stderr must stay empty.

Split out of test_engine_payload.py (file-size ratchet). Registered in
macos-packaging.yml PACKAGING_TESTS; ubuntu ci collects all of tests/.

- if distutils-precedence.pth survives while _distutils_hack is pruned
  then every engine start logs a stderr traceback -> broken.
- if a staged .pth imports a module the payload lacks then verify refuses
  the build naming the file -> broken.
- if runtime/bin/python3 -c pass writes to stderr then verify refuses the
  payload -> broken.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    RUNTIME_PRUNE_RELATIVE,
    PayloadBuildError,
    parse_pth_imports,
    verify_pth_imports,
    verify_python_startup_stderr,
)


def _fake_payload_runtime(tmp_path: Path) -> Path:
    """Minimal payload layout: isolated venv whose prefix is runtime/."""
    payload = tmp_path / "payload"
    runtime = payload / "runtime"
    result = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(runtime)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not create fake payload venv:\n{result.stderr}"
        )
    return payload


def _site_packages(payload: Path) -> Path:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return payload / f"runtime/lib/python{version}/site-packages"


@pytest.mark.requirement("INSTALL-22")
def test_runtime_prune_list_drops_distutils_precedence_pth() -> None:
    assert "lib/python3.*/site-packages/_distutils_hack" in RUNTIME_PRUNE_RELATIVE
    assert (
        "lib/python3.*/site-packages/distutils-precedence.pth"
        in RUNTIME_PRUNE_RELATIVE
    )
    hack_index = RUNTIME_PRUNE_RELATIVE.index(
        "lib/python3.*/site-packages/_distutils_hack"
    )
    pth_index = RUNTIME_PRUNE_RELATIVE.index(
        "lib/python3.*/site-packages/distutils-precedence.pth"
    )
    assert pth_index == hack_index + 1


@pytest.mark.requirement("INSTALL-22")
def test_parse_pth_imports_reads_plain_import(tmp_path: Path) -> None:
    pth = tmp_path / "distutils-precedence.pth"
    pth.write_text("import _distutils_hack\n", encoding="utf-8")
    assert parse_pth_imports(pth) == ["_distutils_hack"]


@pytest.mark.requirement("INSTALL-22")
def test_parse_pth_imports_reads_dunder_import(tmp_path: Path) -> None:
    pth = tmp_path / "distutils-precedence.pth"
    pth.write_text("import os; __import__('_distutils_hack')\n", encoding="utf-8")
    assert parse_pth_imports(pth) == ["os", "_distutils_hack"]


@pytest.mark.requirement("INSTALL-22")
def test_parse_pth_imports_ignores_comments_and_blanks(tmp_path: Path) -> None:
    pth = tmp_path / "empty.pth"
    pth.write_text("# import foo\n\n", encoding="utf-8")
    assert parse_pth_imports(pth) == []


@pytest.mark.requirement("INSTALL-22")
def test_verify_pth_imports_passes_when_import_resolves(tmp_path: Path) -> None:
    payload = _fake_payload_runtime(tmp_path)
    (_site_packages(payload) / "ok.pth").write_text("import json\n", encoding="utf-8")
    verify_pth_imports(payload)


@pytest.mark.requirement("INSTALL-22")
def test_verify_pth_imports_fails_when_import_missing(tmp_path: Path) -> None:
    payload = _fake_payload_runtime(tmp_path)
    (_site_packages(payload) / "bad.pth").write_text(
        "import _distutils_hack\n",
        encoding="utf-8",
    )
    with pytest.raises(PayloadBuildError) as excinfo:
        verify_pth_imports(payload)
    message = str(excinfo.value)
    assert "bad.pth" in message
    assert "_distutils_hack" in message


@pytest.mark.requirement("INSTALL-22")
def test_verify_python_startup_stderr_fails_on_distutils_precedence(
    tmp_path: Path,
) -> None:
    payload = _fake_payload_runtime(tmp_path)
    (_site_packages(payload) / "distutils-precedence.pth").write_text(
        "import _distutils_hack\n",
        encoding="utf-8",
    )
    with pytest.raises(PayloadBuildError) as excinfo:
        verify_python_startup_stderr(payload)
    assert "_distutils_hack" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-22")
def test_verify_python_startup_stderr_passes_when_clean(tmp_path: Path) -> None:
    payload = _fake_payload_runtime(tmp_path)
    verify_python_startup_stderr(payload)
