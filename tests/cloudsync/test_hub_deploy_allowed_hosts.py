"""MUSIC_DJ_ALLOWED_HOSTS render, probe, and status CLI (CLOUDSYNC-18).

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


@pytest.mark.requirement("CLOUDSYNC-18")
@pytest.mark.parametrize("kind", ["systemd", "launchd"])
def test_rendered_serve_unit_sets_allowed_hosts_when_opted_in(kind: str, tmp_path: Path) -> None:
    """if opted-in render lacks MUSIC_DJ_ALLOWED_HOSTS on the serve unit then broken"""
    inputs = _inputs(tmp_path, allowed_hosts=ALLOWED_HOST)
    units = hub_deploy.render_units(kind, inputs)
    hub_name = "opendj-hub.service" if kind == "systemd" else "com.opendj.hub.plist"
    if kind == "systemd":
        env = dict(shlex.split(value)[0].split("=", 1) for value in _systemd_key(units[hub_name], "Environment"))
        backup_text = units["opendj-hub-backup.service"]
    else:
        hub = plistlib.loads(units[hub_name].encode())
        env = hub["EnvironmentVariables"]
        backup = plistlib.loads(units["com.opendj.hub-backup.plist"].encode())
        backup_text = backup.get("EnvironmentVariables", {})
    assert env[ALLOWED_HOSTS_ENV] == ALLOWED_HOST
    assert env[machine_identity.IS_HUB_ENV] == "1"
    if kind == "systemd":
        assert ALLOWED_HOSTS_ENV not in backup_text
    else:
        assert ALLOWED_HOSTS_ENV not in backup_text


@pytest.mark.requirement("CLOUDSYNC-18")
def test_rendered_serve_unit_normalizes_multi_host_allowed_hosts(tmp_path: Path) -> None:
    """if multi-host --allowed-hosts is not normalized by parse_allowed_hosts then broken"""
    raw = f" {ALLOWED_HOST} , other.example-tailnet.ts.net "
    inputs = _inputs(tmp_path, allowed_hosts=raw)
    service = hub_deploy.render_units("systemd", inputs)["opendj-hub.service"]
    env = dict(shlex.split(value)[0].split("=", 1) for value in _systemd_key(service, "Environment"))
    assert env[ALLOWED_HOSTS_ENV] == f"{ALLOWED_HOST},other.example-tailnet.ts.net"


@pytest.mark.requirement("CLOUDSYNC-18")
@pytest.mark.parametrize(
    "bad,match",
    [
        ("https://box.example-tailnet.ts.net", "must list bare hostnames"),
        ("box.example-tailnet.ts.net:443", "must list bare hostnames"),
        (",", "named no hostname"),
    ],
)
def test_invalid_allowed_hosts_fails_at_render(bad: str, match: str) -> None:
    """if invalid --allowed-hosts reaches request time instead of render then broken"""
    with pytest.raises(hub_deploy.HubDeployError, match=match):
        hub_deploy.hub_service_env(allowed_hosts=bad)


@pytest.mark.requirement("CLOUDSYNC-18")
def test_render_cli_refuses_invalid_allowed_hosts(tmp_path: Path) -> None:
    """if render --allowed-hosts with a scheme exits 0 then broken"""
    result = subprocess.run(
        [
            _uv(),
            "run",
            "--no-sync",
            "python",
            "-m",
            "apps.sync_hub.hub_deploy",
            "render",
            "--kind",
            "systemd",
            "--out-dir",
            str(tmp_path),
            "--repo-root",
            str(REPO_ROOT),
            "--uv",
            _uv(),
            "--data-dir",
            str(tmp_path / "opendj-hub"),
            "--port",
            str(hub_deploy.CFG.HUB_PORT),
            "--backup-dest",
            str(tmp_path / "backups"),
            "--keep",
            "14",
            "--allowed-hosts",
            "https://box.example-tailnet.ts.net",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "must list bare hostnames" in result.stderr


@pytest.mark.requirement("CLOUDSYNC-18")
def test_probe_allowed_host_critical_without_env(tmp_path: Path) -> None:
    """if allowlist probe reads OK when the hub lacks MUSIC_DJ_ALLOWED_HOSTS then broken"""
    hub = _boot(tmp_path, "allow-probe-miss", None)
    try:
        _wait_for_hub(hub)
        probe = hub_deploy.probe_allowed_host(hub.url, PROBE_HOST)
        assert probe.verdict == "CRITICAL"
        assert probe.http_status == 403
    finally:
        _stop_hub(hub)


@pytest.mark.requirement("CLOUDSYNC-18")
def test_probe_allowed_host_ok_with_matching_env(tmp_path: Path) -> None:
    """if allowlist probe stays CRITICAL when the hub has the matching env then broken"""
    hub = _boot(tmp_path, "allow-probe-hit", None, allowed_hosts=PROBE_HOST)
    try:
        _wait_for_hub(hub)
        probe = hub_deploy.probe_allowed_host(hub.url, PROBE_HOST)
        assert probe.verdict == "OK"
        assert probe.http_status == 200
    finally:
        _stop_hub(hub)


@pytest.mark.requirement("CLOUDSYNC-18")
def test_probe_allowed_host_unknown_on_closed_port() -> None:
    """if allowlist probe reads OK against a closed port then broken"""
    probe = hub_deploy.probe_allowed_host(f"http://127.0.0.1:{_free_loopback_port()}", PROBE_HOST)
    assert probe.verdict == "UNKNOWN"
    assert probe.verdict != "OK"


@pytest.mark.requirement("CLOUDSYNC-18")
def test_status_cli_allowed_host_probe_without_env(tmp_path: Path) -> None:
    """if status --allowed-hosts exits 0 when the hub lacks the allowlist then broken"""
    hub = _boot(tmp_path, "status-allow-miss", None)
    try:
        _wait_for_hub(hub)
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
                "--allowed-hosts",
                PROBE_HOST,
            ],
            cwd=REPO_ROOT,
            env=_hub_env("0"),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "[CRITICAL] allowed-host:" in result.stdout
    finally:
        _stop_hub(hub)


@pytest.mark.requirement("CLOUDSYNC-18")
def test_status_cli_allowed_host_probe_with_env(tmp_path: Path) -> None:
    """if status --allowed-hosts exits non-zero when the hub has the allowlist then broken"""
    hub = _boot(tmp_path, "status-allow-hit", None, allowed_hosts=PROBE_HOST)
    try:
        _wait_for_hub(hub)
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
                "--allowed-hosts",
                PROBE_HOST,
            ],
            cwd=REPO_ROOT,
            env=_hub_env("0"),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert "[OK] allowed-host:" in result.stdout
    finally:
        _stop_hub(hub)


@pytest.mark.requirement("CLOUDSYNC-18")
def test_hub_service_env_matches_rendered_serve_unit(tmp_path: Path) -> None:
    """if dropping MUSIC_DJ_ALLOWED_HOSTS from opted-in render would still pass then broken"""
    inputs = _inputs(tmp_path, allowed_hosts=ALLOWED_HOST)
    expected = hub_deploy.hub_service_env(allowed_hosts=ALLOWED_HOST)
    service = hub_deploy.render_units("systemd", inputs)["opendj-hub.service"]
    env = dict(shlex.split(value)[0].split("=", 1) for value in _systemd_key(service, "Environment"))
    assert env == expected
    assert ALLOWED_HOSTS_ENV in env
