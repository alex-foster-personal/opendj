"""Shared helpers for hub deploy integration tests.

Boot fixtures, render inputs, and subprocess helpers used by sibling
``test_hub_deploy_*.py`` modules. Register with ``pytest_plugins`` in
``test_hub_deploy.py`` so the ``booted_hubs`` fixture is visible everywhere.
"""

from __future__ import annotations

import functools
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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.__main__ import HUB_MACHINE_NAME_ENV
from apps.shared.allowed_hosts import ALLOWED_HOSTS_ENV
from apps.shared.state import db as state_db
from apps.shared.state import machine_identity, sync_stamp
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, hub_deploy
from tests.waits import wait_for_external_state

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
GOLDEN_FIXTURES: Path = Path(__file__).resolve().parent.parent / "fixtures" / "hub_deploy" / "default"
GOLDEN_REPO: Path = Path("/golden/music-dj-tools")
GOLDEN_UV: str = "/usr/local/bin/uv"
BOOT_GUARD_S: float = 120.0
# Boots tried when the hub loses its port to another process (see `_boot`).
_BIND_ATTEMPTS = 3
#: Ambient env a developer shell may carry that would change what the hub is.
AMBIENT_ENV_TO_DROP: tuple[str, ...] = (
    "VIRTUAL_ENV",
    "MDT_DATA_DIR",
    "MDT_SYNC_TRUST_TAILNET",
    "MDT_CRATE_ROOT",
    "WEB_CONCURRENCY",
    "OPENDJ_ENGINE_WARN_LOG",
    "OPENDJ_ENGINE_LOG_BOOT_ID",
    HUB_MACHINE_NAME_ENV,
)


@dataclass(frozen=True)
class BootedHub:
    url: str
    data_dir: Path
    argv: list[str]
    proc: subprocess.Popen[bytes]
    log_path: Path


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _uv() -> str:
    uv = shutil.which("uv")
    assert uv is not None, "uv must be on PATH: the hub command is `uv run`"
    return uv


def _hub_env(
    is_hub_override: str | None,
    *,
    machine_name_env: str | None = None,
    allowed_hosts: str | None = None,
) -> dict[str, str]:
    """The launcher env: HUB_ENV alone decides MDT_IS_HUB unless a control overrides it."""
    dropped = (
        *AMBIENT_ENV_TO_DROP,
        machine_identity.IS_HUB_ENV,
        HUB_MACHINE_NAME_ENV,
        ALLOWED_HOSTS_ENV,
    )
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env.update(hub_deploy.HUB_ENV)
    if is_hub_override is not None:
        env[machine_identity.IS_HUB_ENV] = is_hub_override
    if machine_name_env is not None:
        env[HUB_MACHINE_NAME_ENV] = machine_name_env
    if allowed_hosts is not None:
        env[ALLOWED_HOSTS_ENV] = allowed_hosts
    return env


def _init_data_dir(data_dir: Path) -> None:
    """Exactly what scripts/cloudsync_hub.sh start runs before the unit."""
    result = subprocess.run(
        [
            _uv(),
            "run",
            "--no-sync",
            "python",
            "-m",
            "apps.sync_hub.hub_deploy",
            "init",
            "--data-dir",
            str(data_dir),
        ],
        cwd=REPO_ROOT,
        env=_hub_env("1"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"init failed: {result.stderr}"


def _logged_boot_id(hub: BootedHub) -> str | None:
    """The boot_id this hub printed on its `[OK] opendj engine` line, once it has."""
    for line in hub.log_path.read_text().splitlines():
        if line.startswith("[OK] opendj engine ") and " boot_id=" in line:
            return line.split(" boot_id=", 1)[1].split()[0]
    return None


def _answers_health(hub: BootedHub) -> bool:
    """True only when THIS hub answers: health must carry the boot_id it logged.

    A 200 alone is not enough. Main push CI run 40413411224 (Mon 26 Oct 2026,
    shard 3) booted a hub whose free port another process on the shared runner
    had already taken: that other process answered health, so the boot passed,
    and this hub then exited 3 with "address already in use" before the first
    test ran. Matching the boot_id keeps waiting on an impostor's answer until
    this hub exits, which `_boot` then retries as a lost port race.
    """
    if hub.proc.poll() is not None:
        raise AssertionError(f"hub exited {hub.proc.returncode}: {hub.log_path.read_text()[-2000:]}")
    boot_id = _logged_boot_id(hub)
    if boot_id is None:
        return False
    try:
        with urllib.request.urlopen(f"{hub.url}/api/v1/health", timeout=2) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read())
    except (OSError, ValueError):
        # ValueError covers a non-JSON 200 from whatever else holds the port.
        return False
    return isinstance(body, dict) and body.get("boot_id") == boot_id


def _boot(
    root: Path,
    name: str,
    is_hub_override: str | None,
    *,
    machine_name: str | None = None,
    machine_name_env: str | None = None,
    allowed_hosts: str | None = None,
    first_port: int | None = None,
) -> BootedHub:
    """Start a hub and wait until it answers health.

    `_free_loopback_port` releases its port before the hub binds it, so another
    process on a shared runner can take it in between: main push CI run
    39004860947 lost that race ("address already in use", exit 3). The hub is a
    `uv run` subprocess serving the production argv, so it cannot be handed a
    pre-bound socket; a boot that lost the race is started again on a fresh
    port instead, and any other failure is raised as it is. `first_port` lets a
    test hand the first attempt a port it knows is taken.
    """
    data_dir = root / name / hub_deploy.CFG.DATA_DIR_NAME
    data_dir.mkdir(parents=True)
    _init_data_dir(data_dir)
    lost: list[int] = []
    for _attempt in range(_BIND_ATTEMPTS):
        port = first_port if first_port is not None and not lost else _free_loopback_port()
        hub = _start_hub(
            root,
            name,
            data_dir,
            port,
            is_hub_override,
            machine_name=machine_name,
            machine_name_env=machine_name_env,
            allowed_hosts=allowed_hosts,
        )
        try:
            _wait_for_hub(hub)
        except AssertionError:
            if not _lost_port_race(hub):
                _stop_hub(hub)
                raise
            lost.append(port)
            continue
        except BaseException:
            _stop_hub(hub)
            raise
        return hub
    raise AssertionError(f"{name} lost the loopback port race {len(lost)} times: ports {lost}")


def _start_hub(
    root: Path,
    name: str,
    data_dir: Path,
    port: int,
    is_hub_override: str | None,
    *,
    machine_name: str | None = None,
    machine_name_env: str | None = None,
    allowed_hosts: str | None = None,
) -> BootedHub:
    """Launch one hub on `port` with the production argv; no wait, no retry."""
    argv = hub_deploy.hub_serve_argv(uv=_uv(), data_dir=data_dir, port=port)
    if machine_name is not None:
        argv.extend(["--machine-name", machine_name])
    log_path = root / f"{name}.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=_hub_env(
                is_hub_override,
                machine_name_env=machine_name_env,
                allowed_hosts=allowed_hosts,
            ),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    return BootedHub(f"http://127.0.0.1:{port}", data_dir, argv, proc, log_path)


# How uvicorn words a refused bind, lowercased: POSIX EADDRINUSE, then
# Windows WinError 10048. Only the POSIX text is driven here (Linux CI).
_BIND_COLLISION_TEXT = ("address already in use", "only one usage of each socket address")


def _lost_port_race(hub: BootedHub) -> bool:
    """True only when the hub exited because its own port was already bound.

    One log line must carry both the collision text and the exact endpoint the
    hub was given, so an unrelated failure that merely mentions a bound address
    is raised as it is rather than retried. The exit status is not matched: the
    same refused bind exits 3 in main CI run 39004860947 and 1 on a cloud host.
    """
    if hub.proc.poll() is None:
        return False
    port = hub.url.rsplit(":", 1)[1]
    return any(
        f"('127.0.0.1', {port})" in line and any(text in line.lower() for text in _BIND_COLLISION_TEXT)
        for line in hub.log_path.read_text().splitlines()
    )


def _wait_for_hub(hub: BootedHub) -> None:
    wait_for_external_state(
        functools.partial(_answers_health, hub),
        what=f"{hub.url} health",
        guard_s=BOOT_GUARD_S,
    )


def _stop_hub(hub: BootedHub) -> None:
    hub.proc.terminate()
    hub.proc.wait(timeout=30)


def _machine_name_for_id(data_dir: Path, machine_id: str) -> str:
    conn = state_db.open_rw(client.state_db_path(data_dir))
    try:
        row = conn.execute(
            "SELECT name FROM machines WHERE machine_id = ?",
            (machine_id,),
        ).fetchone()
        assert row is not None, f"machine_id {machine_id} missing from hub machines"
        return str(row[0])
    finally:
        conn.close()


def _machine_names(data_dir: Path) -> set[str]:
    conn = state_db.open_rw(client.state_db_path(data_dir))
    try:
        return {str(row[0]) for row in conn.execute("SELECT name FROM machines")}
    finally:
        conn.close()


def _init_spoke_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    state_db.open_rw(client.state_db_path(data_dir)).close()


def _hello(url: str, machine_id: str, name: str) -> dict[str, Any]:
    now = sync_stamp.canonical_now()
    body = json.dumps(
        {
            "machine": {
                "machine_id": machine_id,
                "name": name,
                "platform": "linux",
                "is_hub": False,
                "data_root": f"/nonexistent/{name}",
                "first_seen": now,
                "last_seen": now,
            },
            "schema_version": state_schema.SCHEMA_VERSION,
        }
    ).encode()
    request = urllib.request.Request(
        f"{url}/api/v1/sync/hello",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as resp:
        return json.loads(resp.read())


def _inputs(
    tmp_path: Path,
    *,
    upload_r2: bool = False,
    serve_r2: bool = False,
    serve_doppler_config: str | None = None,
    doppler: str | None = None,
    allowed_hosts: str | None = None,
) -> hub_deploy.RenderInputs:
    if doppler is None:
        doppler = "/usr/local/bin/doppler" if (upload_r2 or serve_r2) else None
    if serve_r2 and serve_doppler_config is None:
        serve_doppler_config = "hub_r2_readonly"
    return hub_deploy.RenderInputs(
        repo_root=REPO_ROOT,
        uv=_uv(),
        data_dir=tmp_path / "opendj-hub",
        port=hub_deploy.CFG.HUB_PORT,
        backup_dest=tmp_path / "opendj-hub-backups",
        keep=hub_deploy.CFG.BACKUP_KEEP,
        upload_r2=upload_r2,
        doppler=doppler,
        serve_r2=serve_r2,
        serve_doppler_config=serve_doppler_config,
        allowed_hosts=allowed_hosts,
    )


def _golden_inputs() -> hub_deploy.RenderInputs:
    return hub_deploy.RenderInputs(
        repo_root=GOLDEN_REPO,
        uv=GOLDEN_UV,
        data_dir=Path("/golden/opendj-hub"),
        port=hub_deploy.CFG.HUB_PORT,
        backup_dest=Path("/golden/opendj-hub-backups"),
        keep=hub_deploy.CFG.BACKUP_KEEP,
        upload_r2=False,
        doppler=None,
        serve_r2=False,
        serve_doppler_config=None,
    )


def _systemd_key(text: str, key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in text.splitlines() if line.startswith(f"{key}=")]


def _systemd_argv(exec_start: str) -> list[str]:
    return [word.replace("%%", "%").replace("$$", "$") for word in shlex.split(exec_start)]


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


ALLOWED_HOST = "box.example-tailnet.ts.net"
PROBE_HOST = "probe.example-tailnet.ts.net"


@pytest.fixture(scope="module")
def booted_hubs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, BootedHub]]:
    root = tmp_path_factory.mktemp("hub-deploy")
    # The hub boots on HUB_ENV exactly as the units carry it; only the negative
    # control overrides MDT_IS_HUB.
    hubs: dict[str, BootedHub] = {}
    try:
        hubs["hub"] = _boot(root, "hub", None)
        hubs["not_hub"] = _boot(root, "not-hub", "0")
        for hub in hubs.values():
            _wait_for_hub(hub)
        yield hubs
    finally:
        for hub in hubs.values():
            _stop_hub(hub)
