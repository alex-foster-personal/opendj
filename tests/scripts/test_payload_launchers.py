"""Hermetic unit tests for the shared payload launcher file writer (issue #13306).

- [if] a payload launcher is written [then] bytes and mode match the template contract, [else stop].
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from scripts import payload_beatgrid
from scripts.build_engine_payload import (
    CLI_LAUNCHER_RELATIVE,
    CLI_LAUNCHER_TEMPLATE,
    ENGINE_LAUNCHER_TEMPLATE,
    LAUNCHER_RELATIVE,
    PAYLOAD_LAUNCHER_PREAMBLE,
    PYTHON_LAUNCHER_RELATIVE,
    PYTHON_LAUNCHER_TEMPLATE,
    write_cli_launcher,
    write_engine_launcher,
    write_python_launcher,
)
from scripts.payload_launchers import write_payload_launcher

pytestmark = pytest.mark.requirement("NATIVE-10")

_SAMPLE = "#!/bin/sh\necho ok\n"


def test_write_payload_launcher_creates_nested_path(tmp_path: Path) -> None:
    """[if] a nested launcher path is written [then] parent dirs exist and bytes match, [else stop]."""
    out = write_payload_launcher(tmp_path, "bin/deep/opendj-test", _SAMPLE)
    assert out == tmp_path / "bin/deep/opendj-test"
    assert out.read_text(encoding="utf-8") == _SAMPLE
    assert os.access(out, os.X_OK)
    assert stat.S_IMODE(out.stat().st_mode) == 0o755


def test_write_payload_launcher_accepts_path_relative_path(tmp_path: Path) -> None:
    """[if] relative_path is a Path [then] the same launcher path is written, [else stop]."""
    rel = Path("bin/from-path")
    out = write_payload_launcher(tmp_path, rel, _SAMPLE)
    assert out == tmp_path / rel
    assert out.read_text(encoding="utf-8") == _SAMPLE


def test_write_engine_launcher_matches_template(tmp_path: Path) -> None:
    """[if] an engine payload launcher is written [then] it matches ENGINE_LAUNCHER_TEMPLATE, [else stop]."""
    out = write_engine_launcher(tmp_path)
    assert out == tmp_path / LAUNCHER_RELATIVE
    assert out.read_text(encoding="utf-8") == ENGINE_LAUNCHER_TEMPLATE
    assert os.access(out, os.X_OK)


def test_write_runner_launcher_matches_template(tmp_path: Path) -> None:
    """[if] a Beat This runner launcher is written [then] it matches RUNNER_LAUNCHER_TEMPLATE, [else stop]."""
    out = payload_beatgrid.write_runner_launcher(tmp_path)
    assert out == tmp_path / payload_beatgrid.RUNNER_LAUNCHER_RELATIVE
    assert out.read_text(encoding="utf-8") == payload_beatgrid.RUNNER_LAUNCHER_TEMPLATE
    assert os.access(out, os.X_OK)


def test_launcher_templates_keep_intentional_env_differences() -> None:
    """[if] engine and runner templates differ [then] each keeps its env contract, [else stop]."""
    assert PAYLOAD_LAUNCHER_PREAMBLE in ENGINE_LAUNCHER_TEMPLATE
    assert 'PYTHONPATH="$payload/app:$payload/pylib"' in ENGINE_LAUNCHER_TEMPLATE
    runner = payload_beatgrid.RUNNER_LAUNCHER_TEMPLATE
    assert PAYLOAD_LAUNCHER_PREAMBLE not in runner
    assert "runners/beatgrid/site" in runner
    assert 'PYTHONPATH="$payload/runners/beatgrid/site"' in runner


def test_write_cli_launcher_matches_template(tmp_path: Path) -> None:
    """[if] the CLI launcher is written [then] it matches CLI_LAUNCHER_TEMPLATE, [else stop]."""
    out = write_cli_launcher(tmp_path)
    assert out == tmp_path / CLI_LAUNCHER_RELATIVE
    assert out.read_text(encoding="utf-8") == CLI_LAUNCHER_TEMPLATE
    assert os.access(out, os.X_OK)


def test_write_python_launcher_matches_template(tmp_path: Path) -> None:
    """[if] the python launcher is written [then] it matches PYTHON_LAUNCHER_TEMPLATE, [else stop]."""
    out = write_python_launcher(tmp_path)
    assert out == tmp_path / PYTHON_LAUNCHER_RELATIVE
    assert out.read_text(encoding="utf-8") == PYTHON_LAUNCHER_TEMPLATE
    assert os.access(out, os.X_OK)
