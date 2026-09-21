"""The hub deploy artifacts launch a real hub, and the probe can tell.

Boots the EXACT argv every launcher runs (:func:`hub_deploy.hub_serve_argv`
plus :data:`hub_deploy.HUB_ENV`, after the same ``init`` the start script
runs) as a real subprocess on a free loopback port. Two hubs boot side by
side: one with ``MDT_IS_HUB=1`` and one with ``MDT_IS_HUB=0`` over an
identically initialized data dir, so the probe's ``is_hub`` is shown to come
from the SERVING process and able to say no (.claude/rules/verification.md).

The rendered units are checked against the same argv, so a unit that starts
something other than what this file booted fails here.

[if] hub deploy launches a hub [then] the probes is_hub matches hub_serve_argv, [else stop].
"""

from __future__ import annotations

import functools
import json
import os
import plistlib
import shlex
import shutil
import socket
import subprocess
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.__main__ import HUB_MACHINE_NAME_ENV
from apps.shared.state import db as state_db
from apps.shared.state import machine_identity, sync_stamp
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, hub_deploy
from apps.webui.server.request_guard import ALLOWED_HOSTS_ENV
from tests.waits import wait_for_external_state

pytestmark = pytest.mark.requirement("CAT-04")

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
GOLDEN_FIXTURES: Path = Path(__file__).resolve().parent.parent / "fixtures" / "hub_deploy" / "default"
GOLDEN_REPO: Path = Path("/golden/music-dj-tools")
GOLDEN_UV: str = "/usr/local/bin/uv"
BOOT_GUARD_S: float = 120.0
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


def _answers_health(hub: BootedHub) -> bool:
    if hub.proc.poll() is not None:
        raise AssertionError(
            f"hub exited {hub.proc.returncode}: {hub.log_path.read_text()[-2000:]}"
        )
    try:
        with urllib.request.urlopen(f"{hub.url}/api/v1/health", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def _boot(
    root: Path,
    name: str,
    is_hub_override: str | None,
    *,
    machine_name: str | None = None,
    machine_name_env: str | None = None,
    allowed_hosts: str | None = None,
) -> BootedHub:
    data_dir = root / name / hub_deploy.CFG.DATA_DIR_NAME
    data_dir.mkdir(parents=True)
    _init_data_dir(data_dir)
    port = _free_loopback_port()
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


@pytest.fixture(scope="module")
def booted_hubs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, BootedHub]]:
    root = tmp_path_factory.mktemp("hub-deploy")
    # The hub boots on HUB_ENV exactly as the units carry it; only the negative
    # control overrides MDT_IS_HUB.
    hubs = {"hub": _boot(root, "hub", None), "not_hub": _boot(root, "not-hub", "0")}
    try:
        for hub in hubs.values():
            _wait_for_hub(hub)
        yield hubs
    finally:
        for hub in hubs.values():
            _stop_hub(hub)


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
    assert (
        payload["hub_machine_id"]
        == machine_identity.machine_id_path(hub.data_dir).read_text().strip()
    )
    assert payload["schema_version"] == state_schema.SCHEMA_VERSION


def test_hello_to_the_booted_hub_returns_is_hub_true(booted_hubs: dict[str, BootedHub]) -> None:
    """if a spoke's hello to the launched hub does not see an is_hub machine row then broken"""
    hub = booted_hubs["hub"]
    answer: dict[str, Any] = _hello(hub.url, "pytest-spoke-0001", "pytest-spoke")
    hub_row = next(m for m in answer["machines"] if m["machine_id"] == answer["hub_machine_id"])
    assert hub_row["is_hub"] is True
    assert (
        answer["hub_machine_id"]
        == machine_identity.machine_id_path(hub.data_dir).read_text().strip()
    )
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


# ----- rendered units carry the booted argv ------------------------------------


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


def test_rendered_systemd_unit_runs_the_booted_argv(tmp_path: Path) -> None:
    """if the systemd ExecStart or Environment differs from hub_serve_argv / HUB_ENV then broken"""
    inputs = _inputs(tmp_path)
    units = hub_deploy.render_units("systemd", inputs)
    service = units["opendj-hub.service"]
    expected = hub_deploy.hub_serve_argv(uv=inputs.uv, data_dir=inputs.data_dir, port=inputs.port)
    (exec_start,) = _systemd_key(service, "ExecStart")
    assert _systemd_argv(exec_start) == expected
    env = dict(
        shlex.split(value)[0].split("=", 1) for value in _systemd_key(service, "Environment")
    )
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
    argv = (
        ["plutil", "-lint", *paths]
        if tool == "plutil"
        else ["systemd-analyze", "--user", "verify", *paths]
    )
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


# ----- refusals ----------------------------------------------------------------


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
        hub_deploy.hub_serve_argv(
            uv="/opt/uv", data_dir=Path(bad_dir), port=hub_deploy.CFG.HUB_PORT
        )


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_default_data_dir_is_accepted_as_dedicated(platform: str) -> None:
    """if the documented default data dir is itself refused then broken"""
    data_dir = hub_deploy.default_hub_data_dir(platform, {"HOME": "/home/maintainer"})
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
        default_backup = hub_deploy.render_units("systemd", _inputs(tmp_path))[
            "opendj-hub-backup.service"
        ]
        assert units["opendj-hub-backup.service"] == default_backup
    else:
        hub = plistlib.loads(units[hub_name].encode())
        assert hub["ProgramArguments"] == expected
        default_backup = hub_deploy.render_units("launchd", _inputs(tmp_path))[
            "com.opendj.hub-backup.plist"
        ]
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


# ----- allowed hosts (CLOUDSYNC-18) --------------------------------------------

ALLOWED_HOST = "box.example-tailnet.ts.net"
PROBE_HOST = "probe.example-tailnet.ts.net"


@pytest.mark.requirement("CLOUDSYNC-18")
@pytest.mark.parametrize("kind", ["systemd", "launchd"])
def test_rendered_serve_unit_sets_allowed_hosts_when_opted_in(
    kind: str, tmp_path: Path
) -> None:
    """if opted-in render lacks MUSIC_DJ_ALLOWED_HOSTS on the serve unit then broken"""
    inputs = _inputs(tmp_path, allowed_hosts=ALLOWED_HOST)
    units = hub_deploy.render_units(kind, inputs)
    hub_name = "opendj-hub.service" if kind == "systemd" else "com.opendj.hub.plist"
    if kind == "systemd":
        env = dict(
            shlex.split(value)[0].split("=", 1)
            for value in _systemd_key(units[hub_name], "Environment")
        )
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
    env = dict(
        shlex.split(value)[0].split("=", 1) for value in _systemd_key(service, "Environment")
    )
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
    probe = hub_deploy.probe_allowed_host(
        f"http://127.0.0.1:{_free_loopback_port()}", PROBE_HOST
    )
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
    env = dict(
        shlex.split(value)[0].split("=", 1) for value in _systemd_key(service, "Environment")
    )
    assert env == expected
    assert ALLOWED_HOSTS_ENV in env
