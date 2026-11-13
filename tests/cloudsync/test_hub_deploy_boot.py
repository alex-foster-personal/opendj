"""Real hub boot, status CLI, hello, and is_hub probe cases.

[if] hub deploy launches a hub [then] the probes is_hub matches hub_serve_argv, [else stop].
"""

from __future__ import annotations

import http.server
import json
import os
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.__main__ import HUB_MACHINE_NAME_ENV
from apps.shared.allowed_hosts import ALLOWED_HOSTS_ENV
from apps.shared.state import machine_identity, schema as state_schema
from apps.shared.state import db as state_db
from apps.sync_hub import client, hub_deploy
from tests.cloudsync.hub_deploy_test_support import (
    ALLOWED_HOST,
    AMBIENT_ENV_TO_DROP,
    BOOT_GUARD_S,
    BootedHub,
    GOLDEN_FIXTURES,
    GOLDEN_REPO,
    GOLDEN_UV,
    PROBE_HOST,
    REPO_ROOT,
    _BIND_ATTEMPTS,
    _answers_health,
    _boot,
    _golden_inputs,
    _hello,
    _hub_env,
    _init_data_dir,
    _init_spoke_data_dir,
    _inputs,
    _machine_name_for_id,
    _machine_names,
    _lost_port_race,
    _free_loopback_port,
    _start_hub,
    _stop_hub,
    _systemd_argv,
    _systemd_key,
    _systemd_user_manager_unavailable,
    _uv,
    _wait_for_hub,
    booted_hubs,
)

pytestmark = pytest.mark.requirement("CAT-04")

# ----- the booted hub ------------------------------------------------------------


def test_booted_hub_argv_is_loopback_on_the_dedicated_data_dir(
    booted_hubs: dict[str, BootedHub],
) -> None:
    """if the launched command binds anything but 127.0.0.1 or another data dir then broken"""
    argv = booted_hubs["hub"].argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--data-dir") + 1] == str(booted_hubs["hub"].data_dir)
    assert argv[1:7] == ["run", "--no-sync", "python", "-m", "apps.engine_core", "serve"]


def test_status_recipe_probe_reports_ok_for_the_booted_hub(
    booted_hubs: dict[str, BootedHub],
) -> None:
    """if the status command the recipe runs does not exit 0 with is_hub true then broken"""
    hub = booted_hubs["hub"]
    result = subprocess.run(
        [
            _uv(),
            "run",
            "--no-sync",
            "python",
            "-m",
            "apps.sync_hub.hub_deploy",
            "status",
            "--data-dir",
            str(hub.data_dir),
            "--url",
            hub.url,
        ],
        cwd=REPO_ROOT,
        env=_hub_env("0"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.splitlines()[0])
    assert payload["verdict"] == "OK"
    assert payload["is_hub"] is True
    assert payload["hub_machine_id"] == machine_identity.machine_id_path(hub.data_dir).read_text().strip()
    assert payload["schema_version"] == state_schema.SCHEMA_VERSION


def test_hello_to_the_booted_hub_returns_is_hub_true(booted_hubs: dict[str, BootedHub]) -> None:
    """if a spoke's hello to the launched hub does not see an is_hub machine row then broken"""
    hub = booted_hubs["hub"]
    answer: dict[str, Any] = _hello(hub.url, "pytest-spoke-0001", "pytest-spoke")
    hub_row = next(m for m in answer["machines"] if m["machine_id"] == answer["hub_machine_id"])
    assert hub_row["is_hub"] is True
    assert answer["hub_machine_id"] == machine_identity.machine_id_path(hub.data_dir).read_text().strip()
    spoke_row = next(m for m in answer["machines"] if m["machine_id"] == "pytest-spoke-0001")
    assert spoke_row["is_hub"] is False


def test_probe_reads_critical_when_the_process_lacks_is_hub(
    booted_hubs: dict[str, BootedHub],
) -> None:
    """if the probe says OK for a process started without MDT_IS_HUB=1 then broken"""
    probe = hub_deploy.probe_hub(booted_hubs["not_hub"].url, booted_hubs["not_hub"].data_dir)
    assert probe.verdict == "CRITICAL"
    assert probe.is_hub is False
    assert "MDT_IS_HUB=1" in probe.reason


def test_probe_reads_critical_for_another_hubs_data_dir(booted_hubs: dict[str, BootedHub]) -> None:
    """if the probe accepts a hub that does not own the given data dir then broken"""
    hub, other = booted_hubs["hub"], booted_hubs["not_hub"]
    # Unregistered on this hub: the hub cannot answer for that id, so UNKNOWN.
    assert hub_deploy.probe_hub(hub.url, other.data_dir).verdict == "UNKNOWN"
    other_id = machine_identity.machine_id_path(other.data_dir).read_text().strip()
    _hello(hub.url, other_id, "other-hub-as-spoke")
    probe = hub_deploy.probe_hub(hub.url, other.data_dir)
    assert probe.verdict == "CRITICAL"
    assert "not the one owning" in probe.reason


def test_probe_without_an_answer_is_unknown_never_a_verdict(tmp_path: Path) -> None:
    """if a closed port or a missing machine-id renders as OK or CRITICAL then broken"""
    missing_id = hub_deploy.probe_hub(f"http://127.0.0.1:{_free_loopback_port()}", tmp_path)
    assert missing_id.verdict == "UNKNOWN"
    machine_identity.machine_id_path(tmp_path).write_text("0" * 32)
    closed = hub_deploy.probe_hub(f"http://127.0.0.1:{_free_loopback_port()}", tmp_path)
    assert closed.verdict == "UNKNOWN"
    assert hub_deploy.EXIT_BY_VERDICT[closed.verdict] == 3
