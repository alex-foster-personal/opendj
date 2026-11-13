"""Loopback port race retries and health boot_id impostor guard.

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


def test_a_hub_that_loses_its_port_boots_again_on_a_fresh_one(tmp_path: Path) -> None:
    """[if] another process holds the port the hub was given [then] the boot retries on a fresh port and the hub answers health there"""
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        taken = int(squatter.getsockname()[1])
        hub = _boot(tmp_path, "port-race", None, first_port=taken)
        try:
            assert hub.url != f"http://127.0.0.1:{taken}", "the hub must not claim the squatted port"
            assert _answers_health(hub)
        finally:
            _stop_hub(hub)


def test_only_a_bind_collision_counts_as_a_lost_port_race(tmp_path: Path) -> None:
    """Control, the opposite direction: a real hub that exits for any other reason is not retried"""
    data_dir = tmp_path / "race" / hub_deploy.CFG.DATA_DIR_NAME
    data_dir.mkdir(parents=True)
    _init_data_dir(data_dir)
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        collided = _start_hub(tmp_path, "collided", data_dir, int(squatter.getsockname()[1]), None)
        try:
            collided.proc.wait(timeout=BOOT_GUARD_S)
        finally:
            _stop_hub(collided)
    assert _lost_port_race(collided), collided.log_path.read_text()[-2000:]

    stopped = _start_hub(tmp_path, "stopped", data_dir, _free_loopback_port(), None)
    try:
        _wait_for_hub(stopped)
    finally:
        _stop_hub(stopped)
    assert stopped.proc.returncode is not None
    assert not _lost_port_race(stopped), "a hub stopped after serving did not lose a port race"


# ----- an impostor on the hub's port ----------------------------------------------


def test_health_from_another_process_on_the_port_is_not_this_hub(tmp_path: Path) -> None:
    """if a 200 from whatever holds the port counts as the hub booting then broken"""

    class _Impostor(http.server.BaseHTTPRequestHandler):
        boot_id = "someone-else"
        raw: bytes | None = None

        def do_GET(self) -> None:
            body = self.raw if self.raw is not None else json.dumps({"status": "ok", "boot_id": self.boot_id}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Impostor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        log_path = tmp_path / "hub.log"
        log_path.write_text("[OK] opendj engine 1.0.0 boot_id=mine contract_rev=x data_dir=/d http://127.0.0.1:1\n")
        hub = BootedHub(f"http://127.0.0.1:{server.server_address[1]}", tmp_path, [], sleeper, log_path)
        assert _answers_health(hub) is False
        # Control: the same answer carrying this hub's own boot_id is the hub.
        _Impostor.boot_id = "mine"
        assert _answers_health(hub) is True
        # A 200 that is not JSON (some other service on the port) is not the hub either.
        _Impostor.boot_id = "mine"
        _Impostor.raw = b"<html>not json</html>"
        assert _answers_health(hub) is False
        _Impostor.raw = None
        # Before the hub logs its boot_id there is nothing to match yet.
        log_path.write_text("")
        assert _answers_health(hub) is False
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)
        server.shutdown()
