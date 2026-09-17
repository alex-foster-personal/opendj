"""The payload launchers must find their payload when run through a symlink.

`opendj install-cli` puts ``~/.local/bin/opendj -> <app>/payload/bin/opendj`` on
PATH (AGENT-05). A launcher that derives its payload from ``$0`` without
resolving the link looks for ``~/.local/runtime/bin/python3`` and dies, which is
what the installed c2cabcaff build did on the Air, Tue 15 Sep 2026.

These tests write the REAL launcher templates into a fake payload whose
``runtime/bin/python3`` is a stub that reports what the launcher handed it, so
the assertion is on the paths the launcher resolved, not on the template text.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.build_engine_payload import write_launchers

STUB_PYTHON = """#!/bin/sh
printf 'manifest=%s\\n' "$OPENDJ_PAYLOAD_MANIFEST"
printf 'pythonpath=%s\\n' "$PYTHONPATH"
printf 'argv=%s\\n' "$*"
"""


@pytest.fixture
def payload(tmp_path: Path) -> Path:
    root = tmp_path / "Open DJ.app" / "Contents" / "Resources" / "payload"
    python = root / "runtime" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text(STUB_PYTHON, encoding="utf-8")
    python.chmod(0o755)
    write_launchers(root)
    return root


def _run(launcher: Path, cwd: Path) -> dict[str, str]:
    result = subprocess.run(
        [str(launcher), "--json", "state"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 0, f"launcher failed: {result.stderr}"
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines())


def _assert_resolved_to(report: dict[str, str], payload: Path) -> None:
    real = payload.resolve()
    assert report["manifest"] == str(real / "manifest.json")
    assert report["pythonpath"] == f"{real / 'app'}:{real / 'pylib'}"
    assert report["argv"] == "-m apps.opendj_cli --json state"


def test_cli_launcher_run_directly_finds_its_payload(payload: Path, tmp_path: Path) -> None:
    # Control: the direct path worked before the fix and must keep working.
    _assert_resolved_to(_run(payload / "bin" / "opendj", tmp_path), payload)


def test_cli_launcher_through_absolute_symlink_finds_its_payload(
    payload: Path, tmp_path: Path
) -> None:
    link = tmp_path / "home" / ".local" / "bin" / "opendj"
    link.parent.mkdir(parents=True)
    link.symlink_to(payload / "bin" / "opendj")
    _assert_resolved_to(_run(link, tmp_path), payload)


def test_cli_launcher_through_relative_symlink_chain_finds_its_payload(
    payload: Path, tmp_path: Path
) -> None:
    # Two hops, both relative, and run from an unrelated cwd: a relative link
    # must resolve against the LINK's directory, never the caller's cwd.
    bin_dir = tmp_path / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    hop = bin_dir / "opendj-real"
    hop.symlink_to(os.path.relpath(payload / "bin" / "opendj", bin_dir))
    link = bin_dir / "opendj"
    link.symlink_to("opendj-real")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _assert_resolved_to(_run(link, elsewhere), payload)


def test_engine_launcher_through_symlink_finds_its_payload(payload: Path, tmp_path: Path) -> None:
    link = tmp_path / "opendj-engine"
    link.symlink_to(payload / "bin" / "opendj-engine")
    report = _run(link, tmp_path)
    assert report["manifest"] == str(payload.resolve() / "manifest.json")
    assert report["argv"] == "-m apps.engine_core serve --json state"
