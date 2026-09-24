"""Per-user hub provisioning (#3870): ``hub_deploy provision-user`` / ``remove-user``.

Everything runs against a temp root and a temp unit dir. ``--enable`` and
``remove-user`` drive a recording fake ``systemctl`` so the exact commands are
asserted, and NOTHING here touches agentbox, the tailnet, or a real service
manager. The rendered serve unit is booted once, exactly as systemd would run
it, and probed on /api/v1/health so the unit's ExecStart and Environment are
proven live rather than by string inspection alone.

[if] provision-user runs against a temp root [then] the data dir, hub identity,
three units and registry row exist with enforce on the serve unit, [else stop].
[if] the same user is provisioned twice [then] the port never changes, [else stop].
[if] --dry-run [then] the plan prints and nothing is written, [else stop].
[if] remove-user [then] units disable, files go, registry row goes, data stays, [else stop].
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from apps.engine_core.__main__ import HUB_MACHINE_NAME_ENV
from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub import hub_deploy, hub_user
from apps.sync_hub.machine_credentials import MODE_ENV
from apps.webui.server.request_guard import ALLOWED_HOSTS_ENV

pytestmark = pytest.mark.requirement("CLOUDSYNC-24")

REPO_ROOT = Path(__file__).resolve().parents[2]
UV = "/opt/fake/uv"


# ----- helpers ---------------------------------------------------------------


def _fake_systemctl(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "systemctl.log"
    script = tmp_path / "systemctl"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(log))}\n', encoding="utf-8"
    )
    script.chmod(0o755)
    return str(script), log


def _provision(tmp_path: Path, user: str, **kwargs: object) -> hub_user.UserHubPlan:
    root = tmp_path / "hubs"
    unit_dir = tmp_path / "units"
    plan = hub_user.plan_user_hub(
        user=user,
        root=root,
        port=kwargs.get("port"),  # type: ignore[arg-type]
        unit_dir=unit_dir,
        allowed_hosts=kwargs.get("allowed_hosts"),  # type: ignore[arg-type]
        systemctl=str(kwargs.get("systemctl", "systemctl")),
    )
    hub_user.provision_user_hub(
        plan,
        repo_root=REPO_ROOT,
        uv=UV,
        keep=3,
        allowed_hosts=kwargs.get("allowed_hosts"),  # type: ignore[arg-type]
        enable=bool(kwargs.get("enable", False)),
    )
    return plan


def _unit(plan: hub_user.UserHubPlan, suffix: str) -> str:
    name = f"{hub_user.UCFG.UNIT_PREFIX}{plan.hub.user}{suffix}"
    return Path(plan.unit_files[name]).read_text(encoding="utf-8")


def _env_lines(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("Environment="):
            key, value = line.removeprefix("Environment=").strip('"').split("=", 1)
            pairs[key] = value
    return pairs


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ----- naming ----------------------------------------------------------------


def test_machine_name_env_matches_the_engine_constant() -> None:
    assert hub_user.UCFG.MACHINE_NAME_ENV == HUB_MACHINE_NAME_ENV


@pytest.mark.parametrize("bad", ["", "Ben", "1ben", "a friend", "a", "b" * 33, "../x"])
def test_user_slug_is_validated(bad: str) -> None:
    with pytest.raises(hub_deploy.HubDeployError, match="not a slug"):
        hub_user.validate_user(bad)


def test_unit_names_are_per_user_and_keep_the_single_hub_shape() -> None:
    assert hub_user.unit_names("ben") == {
        "opendj-hub.service": "opendj-hub-ben.service",
        "opendj-hub-backup.service": "opendj-hub-ben-backup.service",
        "opendj-hub-backup.timer": "opendj-hub-ben-backup.timer",
    }


# ----- provision -------------------------------------------------------------


def test_provision_writes_data_dir_identity_units_and_registry(tmp_path: Path) -> None:
    plan = _provision(tmp_path, "ben")
    hub = plan.hub
    assert hub.port == hub_user.UCFG.PORT_BASE
    assert hub.url == f"http://127.0.0.1:{hub.port}"
    assert Path(hub.data_dir) == tmp_path / "hubs" / "ben" / "hub"
    assert Path(hub.backup_dir).is_dir()
    conn = state_db.open_rw(Path(hub.data_dir) / "state" / "state.db")
    try:
        row = conn.execute("SELECT name, is_hub FROM machines").fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("ben-hub", 1)
    assert machine_identity.machine_id_path(Path(hub.data_dir)).is_file()
    for path in plan.unit_files.values():
        assert Path(path).is_file(), path
    service = _unit(plan, ".service")
    env = _env_lines(service)
    assert env[MODE_ENV] == "enforce"
    assert env[HUB_MACHINE_NAME_ENV] == "ben-hub"
    assert env[machine_identity.IS_HUB_ENV] == "1"
    assert f"--port {hub.port}" in service
    assert hub.data_dir in service
    backup = _unit(plan, "-backup.service")
    assert MODE_ENV not in _env_lines(backup)
    assert hub.backup_dir in backup
    timer = _unit(plan, "-backup.timer")
    assert "OnCalendar" in timer
    registry = hub_user.read_registry(tmp_path / "hubs")
    assert registry == {"ben": hub}


def test_serve_unit_argv_is_the_single_hub_argv_on_the_user_port(tmp_path: Path) -> None:
    plan = _provision(tmp_path, "ben")
    service = _unit(plan, ".service")
    (exec_start,) = [
        line.removeprefix("ExecStart=") for line in service.splitlines()
        if line.startswith("ExecStart=")
    ]
    assert shlex.split(exec_start) == hub_deploy.hub_serve_argv(
        uv=UV, data_dir=Path(plan.hub.data_dir), port=plan.hub.port
    )


def test_second_user_gets_the_next_port_and_its_own_dirs(tmp_path: Path) -> None:
    ben = _provision(tmp_path, "ben")
    kim = _provision(tmp_path, "kim")
    assert kim.hub.port == ben.hub.port + 1
    assert kim.hub.data_dir != ben.hub.data_dir
    assert set(kim.unit_files) & set(ben.unit_files) == set()
    assert set(hub_user.read_registry(tmp_path / "hubs")) == {"ben", "kim"}


def test_reprovisioning_the_same_user_keeps_the_port(tmp_path: Path) -> None:
    first = _provision(tmp_path, "ben")
    _provision(tmp_path, "kim")
    again = _provision(tmp_path, "ben")
    assert again.hub == first.hub
    with pytest.raises(hub_deploy.HubDeployError, match="already registered"):
        _provision(tmp_path, "ben", port=first.hub.port + 5)


def test_requested_port_must_be_free_and_in_window(tmp_path: Path) -> None:
    ben = _provision(tmp_path, "ben")
    with pytest.raises(hub_deploy.HubDeployError, match="belongs to ben"):
        _provision(tmp_path, "kim", port=ben.hub.port)
    with pytest.raises(hub_deploy.HubDeployError, match="outside the per-user window"):
        _provision(tmp_path, "kim", port=hub_deploy.CFG.HUB_PORT)


def test_allowed_hosts_gives_the_tailnet_url_and_the_serve_step(tmp_path: Path) -> None:
    plan = _provision(tmp_path, "ben", allowed_hosts="hub.example-tailnet.ts.net")
    assert plan.tailnet_url == f"https://hub.example-tailnet.ts.net:{plan.hub.port}"
    assert _env_lines(_unit(plan, ".service"))[ALLOWED_HOSTS_ENV] == "hub.example-tailnet.ts.net"
    assert plan.expose_commands == [
        ["tailscale", "serve", "--bg", f"--https={plan.hub.port}", plan.hub.url]
    ]


def test_extra_env_cannot_override_the_hub_env() -> None:
    with pytest.raises(hub_deploy.HubDeployError, match="would override"):
        hub_deploy.hub_service_env(extra_env={machine_identity.IS_HUB_ENV: "0"})


def test_enable_runs_daemon_reload_then_enable_now_then_restart(tmp_path: Path) -> None:
    systemctl, log = _fake_systemctl(tmp_path)
    _provision(tmp_path, "ben", systemctl=systemctl, enable=True)
    assert log.read_text(encoding="utf-8").splitlines() == [
        "--user daemon-reload",
        "--user enable --now opendj-hub-ben.service opendj-hub-ben-backup.timer",
        "--user restart opendj-hub-ben.service",
    ]


def test_reprovision_restarts_an_already_active_service(tmp_path: Path) -> None:
    """Codex P1 (PR #3879): a live hub must pick up a rewritten unit file.

    `enable --now` alone leaves an already-active unit running unchanged, so
    a reprovision that changes settings (e.g. --allowed-hosts) would leave
    the live process on its old environment until `restart` runs too.
    """
    systemctl, log = _fake_systemctl(tmp_path)
    _provision(tmp_path, "ben", systemctl=systemctl, enable=True)
    log.write_text("", encoding="utf-8")

    _provision(tmp_path, "ben", systemctl=systemctl, enable=True, allowed_hosts="hub.example.test")

    assert "--user restart opendj-hub-ben.service" in log.read_text(encoding="utf-8").splitlines()


def test_without_enable_no_service_manager_is_called(tmp_path: Path) -> None:
    systemctl, log = _fake_systemctl(tmp_path)
    _provision(tmp_path, "ben", systemctl=systemctl)
    assert not log.exists()


# ----- CLI -------------------------------------------------------------------


def _cli(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "apps.sync_hub.hub_deploy", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


def test_cli_dry_run_prints_the_plan_and_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "hubs"
    proc = _cli(
        "provision-user", "--user", "ben", "--uv", UV, "--repo-root", str(REPO_ROOT),
        "--root", str(root), "--unit-dir", str(tmp_path / "units"), "--dry-run",
    )
    assert proc.returncode == 0, proc.stderr
    plan = json.loads(proc.stdout.splitlines()[0])
    assert plan["hub"]["port"] == hub_user.UCFG.PORT_BASE
    assert "[DRY-RUN] nothing written" in proc.stdout
    assert not root.exists()
    assert not (tmp_path / "units").exists()


def test_cli_provision_prints_the_url_and_next_steps(tmp_path: Path) -> None:
    proc = _cli(
        "provision-user", "--user", "ben", "--uv", UV, "--repo-root", str(REPO_ROOT),
        "--root", str(tmp_path / "hubs"), "--unit-dir", str(tmp_path / "units"),
        "--allowed-hosts", "hub.example-tailnet.ts.net",
    )
    assert proc.returncode == 0, proc.stderr
    port = hub_user.UCFG.PORT_BASE
    assert f"[OK] hub url (loopback): http://127.0.0.1:{port}" in proc.stdout
    tailnet = f"https://hub.example-tailnet.ts.net:{port}"
    assert f"[OK] hub url (tailnet, after tailscale serve): {tailnet}" in proc.stdout
    assert "systemctl --user enable --now opendj-hub-ben.service" in proc.stdout
    assert f"tailscale serve --bg --https={port} http://127.0.0.1:{port}" in proc.stdout


def test_cli_refuses_a_relative_root(tmp_path: Path) -> None:
    proc = _cli(
        "provision-user", "--user", "ben", "--uv", UV, "--repo-root", str(REPO_ROOT),
        "--root", "relative/hubs", "--unit-dir", str(tmp_path / "units"), "--dry-run",
    )
    assert proc.returncode == 1
    assert "[ERROR] --root must be absolute" in proc.stderr


# ----- remove ----------------------------------------------------------------


def test_remove_disables_deletes_units_drops_registry_and_keeps_data(tmp_path: Path) -> None:
    systemctl, log = _fake_systemctl(tmp_path)
    ben = _provision(tmp_path, "ben")
    _provision(tmp_path, "kim")
    removal = hub_user.remove_user_hub(
        user="ben", root=tmp_path / "hubs", unit_dir=tmp_path / "units", systemctl=systemctl
    )
    assert log.read_text(encoding="utf-8").splitlines() == [
        "--user disable --now opendj-hub-ben.service opendj-hub-ben-backup.service "
        "opendj-hub-ben-backup.timer",
        "--user daemon-reload",
    ]
    for path in ben.unit_files.values():
        assert not Path(path).exists(), path
    assert set(hub_user.read_registry(tmp_path / "hubs")) == {"kim"}
    assert removal.kept == [ben.hub.data_dir, ben.hub.backup_dir]
    assert (Path(ben.hub.data_dir) / "state" / "state.db").is_file()


def test_remove_stops_registered_units_even_when_unit_files_are_missing(tmp_path: Path) -> None:
    """A stale unit dir must not leave an orphan process serving the removed user's data."""
    systemctl, log = _fake_systemctl(tmp_path)
    ben = _provision(tmp_path, "ben")
    for path in ben.unit_files.values():
        Path(path).unlink()
    removal = hub_user.remove_user_hub(
        user="ben", root=tmp_path / "hubs", unit_dir=tmp_path / "units", systemctl=systemctl
    )
    assert log.read_text(encoding="utf-8").splitlines() == [
        "--user disable --now opendj-hub-ben.service opendj-hub-ben-backup.service "
        "opendj-hub-ben-backup.timer",
    ]
    assert set(hub_user.read_registry(tmp_path / "hubs")) == set()
    assert removal.kept == [ben.hub.data_dir, ben.hub.backup_dir]


def test_remove_unknown_user_is_refused(tmp_path: Path) -> None:
    (tmp_path / "hubs").mkdir()
    with pytest.raises(hub_deploy.HubDeployError, match="no hub registered"):
        hub_user.remove_user_hub(user="ben", root=tmp_path / "hubs", unit_dir=tmp_path / "units")


def test_freed_port_is_reused_by_the_next_user(tmp_path: Path) -> None:
    systemctl, _ = _fake_systemctl(tmp_path)
    ben = _provision(tmp_path, "ben")
    hub_user.remove_user_hub(
        user="ben", root=tmp_path / "hubs", unit_dir=tmp_path / "units", systemctl=systemctl
    )
    assert _provision(tmp_path, "kim").hub.port == ben.hub.port


# ----- boot the unit as written --------------------------------------------


def test_provisioned_serve_unit_boots_under_enforce(tmp_path: Path) -> None:
    """Run the unit's ExecStart with its Environment; health answers, status 401s."""
    port = _free_port()
    plan = hub_user.plan_user_hub(
        user="ben", root=tmp_path / "hubs", port=None, unit_dir=tmp_path / "units",
        allowed_hosts=None,
    )
    # The registry window may be busy on a shared box: substitute a free port.
    plan = hub_user.UserHubPlan(**{**plan.__dict__, "hub": hub_user.UserHub(
        **{**plan.hub.__dict__, "port": port, "url": f"http://127.0.0.1:{port}"}
    )})
    hub_user.provision_user_hub(
        plan, repo_root=REPO_ROOT, uv=UV, keep=3, allowed_hosts=None, enable=False
    )
    service = _unit(plan, ".service")
    (exec_start,) = [
        line.removeprefix("ExecStart=") for line in service.splitlines()
        if line.startswith("ExecStart=")
    ]
    argv = shlex.split(exec_start)
    assert argv[:4] == [UV, "run", "--no-sync", "python"]
    env = {**os.environ, **_env_lines(service)}
    proc = subprocess.Popen(
        [sys.executable, *argv[4:]],
        cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    stderr = proc.stderr
    assert stderr is not None
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"{plan.hub.url}/api/v1/health", timeout=2) as resp:
                    assert resp.status == 200
                    break
            except (OSError, urllib.error.URLError) as exc:
                if proc.poll() is not None:
                    raise AssertionError(f"hub exited early: {stderr.read()}") from exc
                if time.monotonic() > deadline:
                    raise AssertionError("hub never answered /api/v1/health") from exc
                time.sleep(0.5)
        hub_id = machine_identity.machine_id_path(Path(plan.hub.data_dir)).read_text().strip()
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(
                f"{plan.hub.url}/api/v1/sync/status?machine_id={hub_id}", timeout=5
            )
        assert exc_info.value.code == 401, "ENFORCE must 401 a credential-less status call"
    finally:
        proc.terminate()
        proc.wait(timeout=30)
