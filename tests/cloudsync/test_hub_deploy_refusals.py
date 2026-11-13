"""hub_deploy refusal paths, golden renders, and CLOUDSYNC-17 doppler wrap.

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


@pytest.mark.parametrize(
    "bad_dir",
    [
        "/Users/x/Library/Application Support/com.opendj.desktop",
        "/opt/opendj/releases/abc/data",
        "/var/lib/opendj/releases/abc/data",
        "/srv/releases/opendj-hub",
        "relative/opendj-hub",
    ],
)
def test_hub_refuses_a_data_dir_it_does_not_own(bad_dir: str) -> None:
    """if the hub command accepts the desktop app's or a release's data dir then broken"""
    with pytest.raises(hub_deploy.HubDeployError):
        hub_deploy.hub_serve_argv(uv="/opt/uv", data_dir=Path(bad_dir), port=hub_deploy.CFG.HUB_PORT)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_default_data_dir_is_accepted_as_dedicated(platform: str) -> None:
    """if the documented default data dir is itself refused then broken"""
    data_dir = hub_deploy.default_hub_data_dir(platform, {"HOME": "/home/user"})
    assert data_dir.name == "opendj-hub"
    hub_deploy.assert_dedicated_data_dir(data_dir)


@pytest.mark.requirement("CLOUDSYNC-17")
def test_default_render_matches_main_golden_systemd() -> None:
    """if the default systemd serve unit drifts from pre-change output then broken"""
    rendered = hub_deploy.render_units("systemd", _golden_inputs())["opendj-hub.service"]
    golden = (GOLDEN_FIXTURES / "systemd-opendj-hub.service").read_text(encoding="utf-8")
    assert rendered == golden


@pytest.mark.requirement("CLOUDSYNC-17")
def test_default_render_matches_main_golden_launchd() -> None:
    """if the default launchd serve unit drifts from pre-change output then broken"""
    rendered = hub_deploy.render_units("launchd", _golden_inputs())["com.opendj.hub.plist"]
    golden = (GOLDEN_FIXTURES / "launchd-com.opendj.hub.plist").read_text(encoding="utf-8")
    assert rendered == golden


@pytest.mark.requirement("CLOUDSYNC-17")
@pytest.mark.parametrize("kind", ["systemd", "launchd"])
def test_serve_r2_wraps_serve_unit_argv(kind: str, tmp_path: Path) -> None:
    """if opted-in serve units lack the doppler wrapper then broken"""
    inputs = _inputs(tmp_path, serve_r2=True, serve_doppler_config="hub_r2_readonly")
    units = hub_deploy.render_units(kind, inputs)
    hub_name = "opendj-hub.service" if kind == "systemd" else "com.opendj.hub.plist"
    expected = hub_deploy.hub_serve_argv(
        uv=inputs.uv,
        data_dir=inputs.data_dir,
        port=inputs.port,
        serve_r2=True,
        doppler=inputs.doppler,
        serve_doppler_config="hub_r2_readonly",
    )
    assert expected[:8] == [
        "/usr/local/bin/doppler",
        "run",
        "-p",
        "general",
        "-c",
        "hub_r2_readonly",
        "--",
        inputs.uv,
    ]
    if kind == "systemd":
        (exec_start,) = _systemd_key(units[hub_name], "ExecStart")
        assert _systemd_argv(exec_start) == expected
        default_backup = hub_deploy.render_units("systemd", _inputs(tmp_path))["opendj-hub-backup.service"]
        assert units["opendj-hub-backup.service"] == default_backup
    else:
        hub = plistlib.loads(units[hub_name].encode())
        assert hub["ProgramArguments"] == expected
        default_backup = hub_deploy.render_units("launchd", _inputs(tmp_path))["com.opendj.hub-backup.plist"]
        assert units["com.opendj.hub-backup.plist"] == default_backup


@pytest.mark.requirement("CLOUDSYNC-17")
@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"serve_r2": True, "doppler": "/bin/doppler"}, "--serve-doppler-config"),
        ({"serve_r2": True, "serve_doppler_config": "cfg"}, "--doppler"),
    ],
)
def test_serve_r2_refuses_missing_flags(kwargs: dict[str, object], match: str) -> None:
    """if --serve-r2 proceeds without explicit doppler config or CLI then broken"""
    base = {
        "uv": "/opt/uv",
        "data_dir": Path("/golden/opendj-hub"),
        "port": hub_deploy.CFG.HUB_PORT,
    }
    with pytest.raises(hub_deploy.HubDeployError, match=match):
        hub_deploy.hub_serve_argv(**base, **kwargs)


@pytest.mark.requirement("CLOUDSYNC-17")
def test_render_cli_refuses_serve_r2_without_config(tmp_path: Path) -> None:
    """if render --serve-r2 without --serve-doppler-config exits 0 then broken"""
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
            "--serve-r2",
            "--doppler",
            "/usr/local/bin/doppler",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "--serve-doppler-config" in result.stderr


def test_render_refuses_an_unfilled_placeholder() -> None:
    """if a template renders with a __PLACEHOLDER__ left in it then broken"""
    with pytest.raises(hub_deploy.HubDeployError, match="__PORT__"):
        hub_deploy.render_template("ExecStart=__EXEC__ --port __PORT__", {"EXEC": "/opt/uv"})


def test_init_refuses_without_is_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """if init registers a hub row for a process that is not told it is the hub then broken"""
    monkeypatch.delenv(machine_identity.IS_HUB_ENV, raising=False)
    with pytest.raises(hub_deploy.HubDeployError, match="MDT_IS_HUB=1"):
        hub_deploy.init_hub_data_dir(tmp_path / "opendj-hub")
    assert not (tmp_path / "opendj-hub").exists()
