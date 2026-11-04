"""Runtime host names require explicit env config with no alias default (#5639 stage 2).

Regression one-liners:
  - if MDT_B2STORE_SSH_HOST unset or blank does not raise naming the variable then broken
  - if MDT_B2STORE_SSH_HOST set does not name the host for b2store, b2_stems_to_r2
    and backup_music_to_bifrost2 alike then broken
  - if OPENDJ_ERROR_SINK_LOG set does not win over the job-host sink then broken
  - if the job-host sink is not chosen when ~/jobs/logs exists then broken
  - if the sink falls back anywhere but the per-OS log directory otherwise then broken
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.telemetry.sink import ERROR_SINK_ENV, JOB_HOST_SINK_REL, sink_path
from scripts.b2store import B2STORE_SSH_HOST_ENV, store_ssh_host

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERRIDE_HOST = "asset-store-test-host"


def test_store_host_raises_when_unset_or_blank() -> None:
    """[if] MDT_B2STORE_SSH_HOST unset or blank does not raise [then] fail, [else stop]."""
    for environ in ({}, {B2STORE_SSH_HOST_ENV: ""}, {B2STORE_SSH_HOST_ENV: "  "}):
        with pytest.raises(ValueError, match=B2STORE_SSH_HOST_ENV):
            store_ssh_host(environ)


def test_store_host_override_names_the_host() -> None:
    """[if] a set MDT_B2STORE_SSH_HOST does not name the store host [then] fail, [else stop]."""
    assert store_ssh_host({B2STORE_SSH_HOST_ENV: OVERRIDE_HOST}) == OVERRIDE_HOST
    assert store_ssh_host({B2STORE_SSH_HOST_ENV: f" {OVERRIDE_HOST}\n"}) == OVERRIDE_HOST


def _fresh_import_then_host(value: str | None) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k != B2STORE_SSH_HOST_ENV}
    if value is not None:
        env[B2STORE_SSH_HOST_ENV] = value
    code = (
        "import importlib, sys\n"
        "import scripts.b2store, scripts.b2_stems_to_r2, scripts.backup_music_to_bifrost2\n"
        "from scripts.b2store import store_ssh_host\n"
        "try:\n"
        "    print(store_ssh_host())\n"
        "except ValueError as exc:\n"
        "    print(exc, file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    return proc.returncode, (proc.stdout or proc.stderr).strip()


@pytest.mark.parametrize("value", [None, "", "  "], ids=["unset", "blank", "spaces"])
def test_store_modules_import_then_raise_without_host(value: str | None) -> None:
    """[if] unset host does not fail on first store_ssh_host after import [then] fail, [else stop]."""
    code, out = _fresh_import_then_host(value)
    assert code != 0
    assert B2STORE_SSH_HOST_ENV in out


def test_store_override_in_fresh_process() -> None:
    """[if] override is not visible after import [then] fail, [else stop]."""
    code, out = _fresh_import_then_host(OVERRIDE_HOST)
    assert code == 0
    assert out.splitlines()[-1] == OVERRIDE_HOST


def test_sink_override_wins_over_the_job_host_sink(tmp_path: Path) -> None:
    """[if] a set OPENDJ_ERROR_SINK_LOG does not pick the sink [then] fail, [else stop]."""
    (tmp_path / JOB_HOST_SINK_REL).parent.mkdir(parents=True)
    chosen = tmp_path / "elsewhere" / "sink.jsonl"
    assert sink_path({ERROR_SINK_ENV: str(chosen)}, home=tmp_path) == chosen
    assert sink_path({ERROR_SINK_ENV: f"  {chosen} "}, home=tmp_path) == chosen


def test_sink_defaults_to_the_job_host_sink_when_its_directory_exists(tmp_path: Path) -> None:
    """[if] ~/jobs/logs exists and the sink is not the job-host JSONL [then] fail, [else stop]."""
    (tmp_path / JOB_HOST_SINK_REL).parent.mkdir(parents=True)
    expected = tmp_path / "jobs" / "logs" / "opendj-error-sink.jsonl"
    assert sink_path({}, home=tmp_path) == expected
    assert sink_path({ERROR_SINK_ENV: " "}, home=tmp_path) == expected


def test_sink_without_job_host_directory_uses_the_os_log_directory(tmp_path: Path) -> None:
    """[if] no ~/jobs/logs and the sink leaves the per-OS log dir [then] fail, [else stop]."""
    if sys.platform == "darwin":
        expected = tmp_path / "Library" / "Logs" / "opendj" / "error-sink.jsonl"
    else:
        expected = tmp_path / ".local" / "share" / "opendj" / "error-sink.jsonl"
    assert sink_path({}, home=tmp_path) == expected
    (tmp_path / "Library" / "Logs").mkdir(parents=True)
    assert sink_path({}, home=tmp_path) == expected
