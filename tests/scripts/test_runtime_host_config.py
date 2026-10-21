"""Runtime host names come from explicit env config, defaults unchanged (issue #5639, batch 1).

Stage 1 of moving hardcoded host aliases to documented variables: each variable,
when set, names the host; unset or blank keeps today's house value, so no machine
changes behavior before it sets the variable (ADR-0317 disposition on #5639).

Regression one-liners:
  - if MDT_B2STORE_SSH_HOST unset or blank is not the house alias then broken
  - if MDT_B2STORE_SSH_HOST set does not name the host for b2store, b2_stems_to_r2
    and backup_music_to_bifrost2 alike then broken
  - if OPENDJ_ERROR_SINK_LOG set does not win over the job-host sink then broken
  - if the job-host sink is not chosen when ~/jobs/logs exists then broken
  - if the sink falls back anywhere but the per-OS log directory otherwise then broken
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.telemetry.sink import ERROR_SINK_ENV, JOB_HOST_SINK_REL, sink_path
from scripts.b2store import B2STORE_SSH_HOST_ENV, DEFAULT_B2STORE_SSH_HOST, store_ssh_host

REPO_ROOT = Path(__file__).resolve().parents[2]
HOUSE_ALIAS = "bifrost2"
OVERRIDE_HOST = "asset-store-test-host"
#: Every module that ssh-es to the asset store, and the attribute it reads the host from.
STORE_HOST_READERS = (
    ("scripts.b2store", "SSH_HOST"),
    ("scripts.b2_stems_to_r2", "SSH_HOST"),
    ("scripts.backup_music_to_bifrost2", "SSH_HOST"),
)


def test_store_host_defaults_to_the_house_alias_when_unset_or_blank() -> None:
    """[if] MDT_B2STORE_SSH_HOST unset or blank is not the house alias [then] fail, [else stop]."""
    assert DEFAULT_B2STORE_SSH_HOST == HOUSE_ALIAS
    assert store_ssh_host({}) == HOUSE_ALIAS
    assert store_ssh_host({B2STORE_SSH_HOST_ENV: ""}) == HOUSE_ALIAS
    assert store_ssh_host({B2STORE_SSH_HOST_ENV: "  "}) == HOUSE_ALIAS


def test_store_host_override_names_the_host() -> None:
    """[if] a set MDT_B2STORE_SSH_HOST does not name the store host [then] fail, [else stop]."""
    assert store_ssh_host({B2STORE_SSH_HOST_ENV: OVERRIDE_HOST}) == OVERRIDE_HOST
    assert store_ssh_host({B2STORE_SSH_HOST_ENV: f" {OVERRIDE_HOST}\n"}) == OVERRIDE_HOST


def _hosts_read_in_a_fresh_process(value: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != B2STORE_SSH_HOST_ENV}
    if value is not None:
        env[B2STORE_SSH_HOST_ENV] = value
    code = (
        "import importlib, json, sys\n"
        f"readers = {STORE_HOST_READERS!r}\n"
        "print(json.dumps({m: getattr(importlib.import_module(m), a) for m, a in readers}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, HOUSE_ALIAS), ("", HOUSE_ALIAS), (OVERRIDE_HOST, OVERRIDE_HOST)],
    ids=["unset", "blank", "override"],
)
def test_every_store_reader_follows_the_one_variable(value: str | None, expected: str) -> None:
    """[if] any asset-store module reads its host from anywhere else [then] fail, [else stop]."""
    hosts = _hosts_read_in_a_fresh_process(value)
    assert hosts == {module: expected for module, _attr in STORE_HOST_READERS}


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
    mac_logs = tmp_path / "Library" / "Logs" / "opendj" / "error-sink.jsonl"
    assert sink_path({}, home=tmp_path) == mac_logs
