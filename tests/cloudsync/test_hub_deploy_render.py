"""Rendered launchd/systemd units, lint, and hub machine name (CLOUDSYNC-08).

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


def test_rendered_systemd_unit_runs_the_booted_argv(tmp_path: Path) -> None:
    """if the systemd ExecStart or Environment differs from hub_serve_argv / HUB_ENV then broken"""
    inputs = _inputs(tmp_path)
    units = hub_deploy.render_units("systemd", inputs)
    service = units["opendj-hub.service"]
    expected = hub_deploy.hub_serve_argv(uv=inputs.uv, data_dir=inputs.data_dir, port=inputs.port)
    (exec_start,) = _systemd_key(service, "ExecStart")
    assert _systemd_argv(exec_start) == expected
    env = dict(shlex.split(value)[0].split("=", 1) for value in _systemd_key(service, "Environment"))
    assert env == hub_deploy.HUB_ENV
    assert env[machine_identity.IS_HUB_ENV] == "1"  # presence: the unit starts a HUB
    assert _systemd_key(units["opendj-hub-backup.timer"], "OnCalendar") == ["*-*-* 03:30:00 UTC"]


def test_systemd_quoting_round_trips_spaces_and_specifiers() -> None:
    """if a data dir with spaces or a percent sign renders to a different argv then broken"""
    argv = ["/opt/uv", "--data-dir", "/home/dev b/100% hub", "$HOME"]
    assert _systemd_argv(hub_deploy.systemd_exec_line(argv)) == argv


def test_rendered_launchd_plists_run_the_booted_argv(tmp_path: Path) -> None:
    """if launchd ProgramArguments or EnvironmentVariables differ from the booted run then broken"""
    inputs = _inputs(tmp_path, upload_r2=True)
    units = hub_deploy.render_units("launchd", inputs)
    hub = plistlib.loads(units["com.opendj.hub.plist"].encode())
    assert hub["ProgramArguments"] == hub_deploy.hub_serve_argv(
        uv=inputs.uv, data_dir=inputs.data_dir, port=inputs.port
    )
    assert hub["EnvironmentVariables"] == hub_deploy.HUB_ENV
    assert hub["EnvironmentVariables"][machine_identity.IS_HUB_ENV] == "1"
    assert hub["WorkingDirectory"] == str(REPO_ROOT)
    backup = plistlib.loads(units["com.opendj.hub-backup.plist"].encode())
    assert backup["ProgramArguments"][:7] == [
        "/usr/local/bin/doppler",
        "run",
        "-p",
        "general",
        "-c",
        "dev_personal",
        "--",
    ]
    assert backup["ProgramArguments"][-1] == "--upload-r2"
    assert backup["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}


_SYSTEMD_MANAGER_INIT_FAILURE_MARKERS: tuple[str, ...] = (
    "failed to initialize manager",
    "failed to lookup runtimedirectory path",
    "failed to connect to bus",
)


def _systemd_user_manager_unavailable() -> str | None:
    """``None`` if ``systemd-analyze --user verify`` can measure at all.

    Otherwise the stderr+stdout that proves the USER MANAGER itself could not
    start on this host (no XDG_RUNTIME_DIR / logind session, common on a
    self-hosted CI runner with no login session) -- never a verdict on a unit
    file. Probes a trivial, syntactically-valid unit that ships with systemd
    itself (``systemd-analyze --user verify`` accepts a bare unit NAME too),
    so a genuine lint failure on the units under test still fails loudly: this
    only returns non-None when the manager cannot initialize AT ALL, not on
    any nonzero exit.
    """
    probe = subprocess.run(
        ["systemd-analyze", "--user", "verify", "systemd-user-sessions.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (probe.stdout + probe.stderr).lower()
    if any(marker in output for marker in _SYSTEMD_MANAGER_INIT_FAILURE_MARKERS):
        return probe.stdout + probe.stderr
    return None


@pytest.mark.parametrize("tool", ["plutil", "systemd-analyze"])
def test_rendered_units_pass_the_service_manager_lint(tool: str, tmp_path: Path) -> None:
    """if plutil -lint or systemd-analyze --user verify rejects a rendered unit then broken"""
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} is not installed on this host; the other lint covers its platform")
    if tool == "systemd-analyze":
        unavailable = _systemd_user_manager_unavailable()
        if unavailable is not None:
            pytest.skip(f"systemd user manager unavailable on this runner: {unavailable}")
    kind: hub_deploy.Kind = "launchd" if tool == "plutil" else "systemd"
    out_dir = tmp_path / kind
    out_dir.mkdir()
    paths = []
    for name, text in hub_deploy.render_units(kind, _inputs(tmp_path)).items():
        (out_dir / name).write_text(text)
        paths.append(str(out_dir / name))
    argv = ["plutil", "-lint", *paths] if tool == "plutil" else ["systemd-analyze", "--user", "verify", *paths]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


# ----- hub machine name (CLOUDSYNC-08) -----------------------------------------


@pytest.mark.requirement("CLOUDSYNC-08")
def test_explicit_machine_name_wins_over_env_and_co_hosts_with_hostname_spoke(
    tmp_path: Path,
) -> None:
    """[if] engine_core serve gets --machine-name while MDT_HUB_MACHINE_NAME differs [then] the hub registers under the flag and a same-host spoke using the hostname syncs without 409."""
    cli_name = "pytest-cli-hub-name"
    env_name = "pytest-env-hub-name-wrong"
    spoke_name = machine_identity.default_machine_name()
    hub = _boot(
        tmp_path,
        "named-hub",
        None,
        machine_name=cli_name,
        machine_name_env=env_name,
    )
    spoke_dir = tmp_path / "spoke"
    try:
        _wait_for_hub(hub)
        _init_spoke_data_dir(spoke_dir)
        client.run_sync(spoke_dir, hub.url, name=spoke_name)
        hub_id = machine_identity.machine_id_path(hub.data_dir).read_text().strip()
        assert _machine_name_for_id(hub.data_dir, hub_id) == cli_name
        names = _machine_names(hub.data_dir)
        assert cli_name in names
        assert spoke_name in names
        assert env_name not in names
        assert "--machine-name" in hub.argv
    finally:
        _stop_hub(hub)


@pytest.mark.requirement("CLOUDSYNC-08")
def test_machine_name_env_fallback_registers_hub_without_cli_flag(tmp_path: Path) -> None:
    """[if] MDT_HUB_MACHINE_NAME is set and --machine-name is omitted [then] the hub registers under the environment value."""
    env_name = "pytest-env-only-hub"
    spoke_name = "pytest-env-spoke"
    hub = _boot(tmp_path, "env-hub", None, machine_name_env=env_name)
    try:
        _wait_for_hub(hub)
        answer = _hello(hub.url, "pytest-env-spoke-0001", spoke_name)
        hub_id = machine_identity.machine_id_path(hub.data_dir).read_text().strip()
        assert _machine_name_for_id(hub.data_dir, hub_id) == env_name
        assert "--machine-name" not in hub.argv
        hub_row = next(m for m in answer["machines"] if m["machine_id"] == hub_id)
        assert hub_row["name"] == env_name
    finally:
        _stop_hub(hub)


@pytest.mark.requirement("CLOUDSYNC-08")
def test_default_machine_name_when_no_flag_or_env(booted_hubs: dict[str, BootedHub]) -> None:
    """[if] neither --machine-name nor MDT_HUB_MACHINE_NAME is set [then] the hub registers under the short hostname."""
    hub = booted_hubs["hub"]
    spoke_name = "pytest-default-spoke"
    answer = _hello(hub.url, "pytest-default-spoke-0001", spoke_name)
    hub_id = machine_identity.machine_id_path(hub.data_dir).read_text().strip()
    assert _machine_name_for_id(hub.data_dir, hub_id) == machine_identity.default_machine_name()
    assert "--machine-name" not in hub.argv
    hub_row = next(m for m in answer["machines"] if m["machine_id"] == hub_id)
    assert hub_row["name"] == machine_identity.default_machine_name()
