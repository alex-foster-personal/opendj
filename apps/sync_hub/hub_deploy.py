"""Hub deploy: the ONE place that says how a CloudSync hub is launched.

A hub is a headless ``python -m apps.engine_core serve`` with ``MDT_IS_HUB=1``
on a dedicated data dir, bound to 127.0.0.1 on a fixed port and exposed to
the tailnet only through ``tailscale serve`` (docs/cloudsync/hub-runbook.md).
The packaged desktop app is never the hub: its shell hard-codes the bind.

Everything that launches a hub derives from :func:`hub_serve_argv` and
:data:`HUB_ENV`: the systemd and launchd templates under ``ops/cloudsync`` are
rendered from them, and ``tests/cloudsync/test_hub_deploy.py`` boots exactly
that argv. So the unit a host runs and the command the test proves are one
value, not two copies that drift.

``status`` asks the running hub over HTTP whether it is a hub. It reads
``GET /api/v1/sync/status`` as the hub's OWN machine id rather than calling
``POST /sync/hello``: hello upserts its caller's row unconditionally, so a
hello probe either registers a phantom machine that every spoke then learns,
or (sent as the hub's own id) writes the very ``is_hub`` it claims to check.
``status`` re-registers the hub from the SERVING process's env before
answering, so ``is_hub`` there reflects the process, not the probe. Verdicts
follow the monitoring-plugin convention: 0 OK, 2 CRITICAL, 3 UNKNOWN, and a
probe that cannot measure says UNKNOWN, never a verdict.

CLI::

    python -m apps.sync_hub.hub_deploy default-data-dir
    python -m apps.sync_hub.hub_deploy argv   --uv UV --data-dir D --port P
    python -m apps.sync_hub.hub_deploy render --kind systemd|launchd --out-dir O --repo-root R \\
        --uv UV --data-dir D --port P --backup-dest B --keep N [--upload-r2 --doppler DOPPLER] \\
        [--serve-r2 --doppler DOPPLER --serve-doppler-config CONFIG] \\
        [--allowed-hosts HOST[,HOST]]
    python -m apps.sync_hub.hub_deploy init   --data-dir D
    python -m apps.sync_hub.hub_deploy status --data-dir D --url http://127.0.0.1:P \\
        [--allowed-hosts HOST[,HOST]]
    python -m apps.sync_hub.hub_deploy provision-user --user U ...   (apps/sync_hub/hub_user.py)
    python -m apps.sync_hub.hub_deploy remove-user    --user U ...   (one hub per test user, #3870)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.shared.state import schema as state_schema
from apps.sync_hub import hub_user
from apps.webui.server.request_guard import ALLOWED_HOSTS_ENV, parse_allowed_hosts

Kind = Literal["systemd", "launchd"]
Verdict = Literal["OK", "CRITICAL", "UNKNOWN"]


class CFG:
    HUB_HOST: str = "127.0.0.1"
    #: Outside the worktree backend window (8680-8799) and every entry in
    #: apps/webui/port_config.RESERVED_FIXED_PORTS, so no lane can claim it.
    HUB_PORT: int = 8870
    BACKUP_KEEP: int = 14
    #: systemd reads this in UTC; launchd has no zone field and runs 03:30 local.
    BACKUP_HOUR: int = 3
    BACKUP_MINUTE: int = 30
    DATA_DIR_NAME: str = "opendj-hub"
    LAUNCHD_LABEL: str = "com.opendj.hub"
    LAUNCHD_BACKUP_LABEL: str = "com.opendj.hub-backup"
    DOPPLER_PROJECT: str = "general"
    DOPPLER_CONFIG: str = "dev_personal"
    TEMPLATES_DIR: Path = Path(__file__).resolve().parents[2] / "ops" / "cloudsync"
    #: Data dirs a hub must never adopt: the packaged app's library and the
    #: release daemons' dirs (agentbox /opt and /var/lib opendj trees).
    FORBIDDEN_PATH_PARTS: tuple[str, ...] = ("com.opendj.desktop", "releases")
    FORBIDDEN_ROOTS: tuple[Path, ...] = (Path("/opt/opendj"), Path("/var/lib/opendj"))
    PROBE_TIMEOUT_S: float = 10.0


#: The hub's whole environment contract. MDT_LIBRARY_MODE is required off
#: darwin; a hub serves no audio, so it never arms analyze-on-import.
HUB_ENV: dict[str, str] = {
    machine_identity.IS_HUB_ENV: "1",
    "MDT_LIBRARY_MODE": "local",
    "MUSIC_DJ_AUTO_ANALYZE": "off",
}


def _validated_allowed_hosts(raw: str) -> str:
    """Parse and normalize; fail at render time, not request time."""
    try:
        hosts = parse_allowed_hosts(raw.strip())
    except ValueError as exc:
        raise HubDeployError(str(exc)) from exc
    return ",".join(hosts)


def hub_service_env(
    *, allowed_hosts: str | None = None, extra_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The serve unit's env. ``extra_env`` may add keys, never override one."""
    env = dict(HUB_ENV)
    if allowed_hosts is not None and allowed_hosts.strip():
        env[ALLOWED_HOSTS_ENV] = _validated_allowed_hosts(allowed_hosts)
    for key, value in (extra_env or {}).items():
        if key in env:
            raise HubDeployError(f"extra env {key} would override the hub's {env[key]!r}")
        env[key] = value
    return env

TEMPLATE_FILES: dict[Kind, dict[str, str]] = {
    "systemd": {
        "opendj-hub.service": "opendj-hub.service.template",
        "opendj-hub-backup.service": "opendj-hub-backup.service.template",
        "opendj-hub-backup.timer": "opendj-hub-backup.timer.template",
    },
    "launchd": {
        f"{CFG.LAUNCHD_LABEL}.plist": "com.opendj.hub.plist.template",
        f"{CFG.LAUNCHD_BACKUP_LABEL}.plist": "com.opendj.hub-backup.plist.template",
    },
}

EXIT_BY_VERDICT: dict[Verdict, int] = {"OK": 0, "CRITICAL": 2, "UNKNOWN": 3}
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


class HubDeployError(RuntimeError):
    """A hub deploy step was refused. The message names the condition."""


# ----- data dir --------------------------------------------------------------


def default_hub_data_dir(
    platform: str | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """``~/Library/Application Support/opendj-hub`` or ``$XDG_DATA_HOME/opendj-hub``."""
    plat = sys.platform if platform is None else platform
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home())
    if plat == "darwin":
        return home / "Library" / "Application Support" / CFG.DATA_DIR_NAME
    if plat.startswith("linux"):
        xdg = env.get("XDG_DATA_HOME", "")
        base = Path(xdg) if xdg else home / ".local" / "share"
        return base / CFG.DATA_DIR_NAME
    raise HubDeployError(f"no hub data dir convention for platform {plat!r}; pass --data-dir")


def assert_dedicated_data_dir(data_dir: Path) -> None:
    """A hub owns its data dir: never the desktop app's, never a release's."""
    path = Path(data_dir)
    if not path.is_absolute():
        raise HubDeployError(f"hub data dir must be absolute, got {str(data_dir)!r}")
    shared = [part for part in path.parts if part in CFG.FORBIDDEN_PATH_PARTS]
    under_release_root = [root for root in CFG.FORBIDDEN_ROOTS if path.is_relative_to(root)]
    if shared or under_release_root:
        raise HubDeployError(
            f"{path} is not a dedicated hub data dir (matched {shared + under_release_root}); "
            f"use {CFG.DATA_DIR_NAME} under the user data dir, outside any release dir"
        )


# ----- commands --------------------------------------------------------------


def hub_serve_argv(
    *,
    uv: str,
    data_dir: Path,
    port: int,
    serve_r2: bool = False,
    doppler: str | None = None,
    serve_doppler_config: str | None = None,
) -> list[str]:
    """The exact hub command every launcher runs. Loopback, always."""
    assert_dedicated_data_dir(data_dir)
    argv = [
        uv,
        "run",
        "--no-sync",
        "python",
        "-m",
        "apps.engine_core",
        "serve",
        "--data-dir",
        str(data_dir),
        "--host",
        CFG.HUB_HOST,
        "--port",
        str(port),
    ]
    if not serve_r2:
        return argv
    if not serve_doppler_config:
        raise HubDeployError("--serve-r2 needs --serve-doppler-config")
    if not doppler:
        raise HubDeployError("--serve-r2 needs --doppler (absolute path to the doppler CLI)")
    return _doppler_wrap(doppler, serve_doppler_config, argv)


def _doppler_wrap(doppler: str, config: str, inner: Sequence[str]) -> list[str]:
    return [doppler, "run", "-p", CFG.DOPPLER_PROJECT, "-c", config, "--", *inner]


def hub_backup_argv(
    *, uv: str, data_dir: Path, dest: Path, keep: int, upload_r2: bool, doppler: str | None
) -> list[str]:
    argv = [
        uv,
        "run",
        "--no-sync",
        "python",
        "-m",
        "apps.sync_hub.hub_backup",
        "backup",
        "--data-dir",
        str(data_dir),
        "--dest",
        str(dest),
        "--keep",
        str(keep),
    ]
    if not upload_r2:
        return argv
    if not doppler:
        raise HubDeployError("--upload-r2 needs --doppler (absolute path to the doppler CLI)")
    return _doppler_wrap(doppler, CFG.DOPPLER_CONFIG, [*argv, "--upload-r2"])


# ----- rendering -------------------------------------------------------------


def _systemd_word(arg: str) -> str:
    """Quote one ExecStart word; ``%`` and ``$`` are systemd specifiers."""
    escaped = arg.replace("%", "%%").replace("$", "$$")
    if re.search(r'[\s"\\\']', escaped):
        return '"' + escaped.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return escaped


def systemd_exec_line(argv: Sequence[str]) -> str:
    return " ".join(_systemd_word(arg) for arg in argv)


def systemd_environment(env: Mapping[str, str]) -> str:
    return "\n".join(f'Environment="{key}={value}"' for key, value in sorted(env.items()))


def launchd_strings(argv: Sequence[str]) -> str:
    return "\n".join(f"\t\t<string>{escape(arg)}</string>" for arg in argv)


def launchd_environment(env: Mapping[str, str]) -> str:
    return "\n".join(
        f"\t\t<key>{escape(key)}</key>\n\t\t<string>{escape(value)}</string>"
        for key, value in sorted(env.items())
    )


def render_template(template: str, values: Mapping[str, str]) -> str:
    """Fill ``__KEY__`` tokens. Unfilled or unused keys are errors, not blanks."""
    rendered = template
    for key, value in values.items():
        token = f"__{key}__"
        if token not in rendered:
            raise HubDeployError(f"template has no {token} placeholder")
        rendered = rendered.replace(token, value)
    leftover = sorted(set(_PLACEHOLDER.findall(rendered)))
    if leftover:
        raise HubDeployError(f"template placeholders left unfilled: {leftover}")
    return rendered


@dataclass(frozen=True)
class RenderInputs:
    repo_root: Path
    uv: str
    data_dir: Path
    port: int
    backup_dest: Path
    keep: int
    upload_r2: bool
    doppler: str | None
    serve_r2: bool = False
    serve_doppler_config: str | None = None
    allowed_hosts: str | None = None
    #: Added to the serve unit only (never the backup job): the per-user hub
    #: sets its credential mode and machine name here.
    extra_env: Mapping[str, str] | None = None


def _placeholder_values(kind: Kind, inputs: RenderInputs) -> dict[str, dict[str, str]]:
    serve_env = hub_service_env(allowed_hosts=inputs.allowed_hosts, extra_env=inputs.extra_env)
    serve = hub_serve_argv(
        uv=inputs.uv,
        data_dir=inputs.data_dir,
        port=inputs.port,
        serve_r2=inputs.serve_r2,
        doppler=inputs.doppler,
        serve_doppler_config=inputs.serve_doppler_config,
    )
    backup = hub_backup_argv(
        uv=inputs.uv,
        data_dir=inputs.data_dir,
        dest=inputs.backup_dest,
        keep=inputs.keep,
        upload_r2=inputs.upload_r2,
        doppler=inputs.doppler,
    )
    repo, logs = str(inputs.repo_root), str(inputs.data_dir / "logs")
    if kind == "systemd":
        return {
            "opendj-hub.service": {
                "REPO_ROOT": repo,
                "ENVIRONMENT": systemd_environment(serve_env),
                "EXEC_START": systemd_exec_line(serve),
            },
            "opendj-hub-backup.service": {
                "REPO_ROOT": repo,
                "EXEC_START": systemd_exec_line(backup),
            },
            "opendj-hub-backup.timer": {
                "ON_CALENDAR": f"*-*-* {CFG.BACKUP_HOUR:02d}:{CFG.BACKUP_MINUTE:02d}:00 UTC",
            },
        }
    if kind == "launchd":
        return {
            f"{CFG.LAUNCHD_LABEL}.plist": {
                "LABEL": CFG.LAUNCHD_LABEL,
                "REPO_ROOT": escape(repo),
                "PROGRAM_ARGUMENTS": launchd_strings(serve),
                "ENVIRONMENT": launchd_environment(serve_env),
                "LOG_DIR": escape(logs),
            },
            f"{CFG.LAUNCHD_BACKUP_LABEL}.plist": {
                "LABEL": CFG.LAUNCHD_BACKUP_LABEL,
                "REPO_ROOT": escape(repo),
                "PROGRAM_ARGUMENTS": launchd_strings(backup),
                "LOG_DIR": escape(logs),
                "HOUR": str(CFG.BACKUP_HOUR),
                "MINUTE": str(CFG.BACKUP_MINUTE),
            },
        }
    raise HubDeployError(f"unknown unit kind {kind!r}")


def render_units(kind: Kind, inputs: RenderInputs) -> dict[str, str]:
    """``{installed file name: rendered text}`` for one service manager."""
    values = _placeholder_values(kind, inputs)
    return {
        name: render_template(
            (CFG.TEMPLATES_DIR / template).read_text(encoding="utf-8"), values[name]
        )
        for name, template in TEMPLATE_FILES[kind].items()
    }


# ----- init + status ---------------------------------------------------------


def init_hub_data_dir(
    data_dir: Path, *, env: Mapping[str, str] | None = None, name: str | None = None
) -> machine_identity.MachineIdentity:
    """Create the hub DB and its own ``machines`` row, so ``status`` can read it.

    ``env`` stands in for the process environment (a provisioning CLI passes
    the unit's env rather than exporting ``MDT_IS_HUB`` into its own).
    """
    assert_dedicated_data_dir(data_dir)
    if not machine_identity.is_hub_from_env(env):
        raise HubDeployError(
            f"init must run with {machine_identity.IS_HUB_ENV}=1 in the environment"
        )
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    conn = state_db.open_rw(Path(data_dir) / "state" / "state.db")
    try:
        return machine_identity.register_machine(
            conn, data_dir=Path(data_dir), name=name, is_hub=True
        )
    finally:
        conn.close()


@dataclass(frozen=True)
class HubProbe:
    verdict: Verdict
    reason: str
    url: str
    hub_machine_id: str | None = None
    is_hub: bool | None = None
    schema_version: int | None = None
    seq: int | None = None
    machine_count: int | None = None


def _fetch_status(url: str, machine_id: str) -> dict[str, object]:
    query = urllib.parse.urlencode({"machine_id": machine_id})
    with urllib.request.urlopen(
        f"{url}/api/v1/sync/status?{query}", timeout=CFG.PROBE_TIMEOUT_S
    ) as resp:
        return json.loads(resp.read().decode("utf-8"))


def probe_hub(url: str, data_dir: Path) -> HubProbe:
    """Ask the hub at ``url`` whether it is the hub that owns ``data_dir``."""
    base = url.rstrip("/")
    id_file = machine_identity.machine_id_path(Path(data_dir))
    if not id_file.is_file():
        return HubProbe("UNKNOWN", f"{id_file} is missing; run `hub_deploy init` first", base)
    own_id = id_file.read_text(encoding="utf-8").strip()
    try:
        body = _fetch_status(base, own_id)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return HubProbe("UNKNOWN", f"GET /api/v1/sync/status answered {exc.code}: {detail}", base)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return HubProbe("UNKNOWN", f"no answer from {base}: {exc}", base)
    machines = body.get("machines")
    if not isinstance(machines, list):
        return HubProbe("UNKNOWN", f"status response has no machines list: {str(body)[:200]}", base)
    hub_id = str(body.get("hub_machine_id"))
    row = next((m for m in machines if isinstance(m, dict) and m.get("machine_id") == hub_id), None)
    is_hub = None if row is None else bool(row.get("is_hub"))
    schema = body.get("schema_version")
    seq = body.get("seq")

    def verdict(level: Verdict, reason: str) -> HubProbe:
        return HubProbe(
            level,
            reason,
            base,
            hub_machine_id=hub_id,
            is_hub=is_hub,
            schema_version=schema if isinstance(schema, int) else None,
            seq=seq if isinstance(seq, int) else None,
            machine_count=len(machines),
        )

    if hub_id != own_id:
        return verdict("CRITICAL", f"{base} serves hub {hub_id}, not the one owning {own_id}")
    if not is_hub:
        return verdict("CRITICAL", f"{base} is not running with {machine_identity.IS_HUB_ENV}=1")
    if schema != state_schema.SCHEMA_VERSION:
        return verdict(
            "CRITICAL",
            f"hub schema {schema} != this checkout's {state_schema.SCHEMA_VERSION}; "
            "upgrade hub and spokes in lockstep (runbook, upgrade order)",
        )
    return verdict("OK", "is_hub true, hub id matches the data dir, schema matches")


@dataclass(frozen=True)
class AllowedHostProbe:
    verdict: Verdict
    reason: str
    host: str | None = None
    http_status: int | None = None


def probe_allowed_host(url: str, allowed_hosts: str) -> AllowedHostProbe:
    """Probe loopback health with Host set to the first allowed hostname."""
    base = url.rstrip("/")
    try:
        normalized = _validated_allowed_hosts(allowed_hosts)
    except HubDeployError as exc:
        return AllowedHostProbe("UNKNOWN", str(exc))
    first_host = normalized.split(",", 1)[0]
    request = urllib.request.Request(
        f"{base}/api/v1/health",
        headers={"Host": first_host},
    )
    try:
        with urllib.request.urlopen(request, timeout=CFG.PROBE_TIMEOUT_S) as resp:
            status = resp.status
            body_text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body_text = exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return AllowedHostProbe(
            "UNKNOWN",
            f"no answer from {base} with Host {first_host!r}: {exc}",
            host=first_host,
        )
    if status == 200:
        return AllowedHostProbe(
            "OK",
            f"allowed host {first_host!r} accepted on loopback",
            host=first_host,
            http_status=status,
        )
    if status == 403:
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict) and body.get("code") == "HOST_NOT_ALLOWED":
            return AllowedHostProbe(
                "CRITICAL",
                f"host {first_host!r} is not in the configured allowlist",
                host=first_host,
                http_status=status,
            )
    snippet = body_text[:300]
    return AllowedHostProbe(
        "UNKNOWN",
        f"GET /api/v1/health with Host {first_host!r} answered {status}: {snippet}",
        host=first_host,
        http_status=status,
    )


# ----- CLI -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.sync_hub.hub_deploy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("default-data-dir", help="print this platform's hub data dir")
    argv = sub.add_parser("argv", help="print the hub serve command")
    render = sub.add_parser("render", help="render service-manager units")
    for command in (argv, render):
        command.add_argument("--uv", required=True, help="absolute path to uv")
        command.add_argument("--data-dir", required=True, type=Path)
        command.add_argument("--port", required=True, type=int)
    render.add_argument("--kind", required=True, choices=sorted(TEMPLATE_FILES))
    render.add_argument("--out-dir", required=True, type=Path)
    render.add_argument("--repo-root", required=True, type=Path)
    render.add_argument("--backup-dest", required=True, type=Path)
    render.add_argument("--keep", required=True, type=int)
    render.add_argument("--upload-r2", action="store_true")
    render.add_argument(
        "--doppler", default=None, help="absolute path to doppler (with --upload-r2 or --serve-r2)"
    )
    argv.add_argument(
        "--doppler", default=None, help="absolute path to doppler (with --serve-r2)"
    )
    for command in (argv, render):
        command.add_argument("--serve-r2", action="store_true")
        command.add_argument(
            "--serve-doppler-config",
            default=None,
            help="Doppler config for --serve-r2 (project is CFG.DOPPLER_PROJECT)",
        )
    render.add_argument(
        "--allowed-hosts",
        default=None,
        help="comma-separated bare hostnames for MUSIC_DJ_ALLOWED_HOSTS on the serve unit "
        "(required for tailnet spokes via tailscale serve)",
    )
    init = sub.add_parser(
        "init", help="create the hub DB and register the hub (needs MDT_IS_HUB=1)"
    )
    init.add_argument("--data-dir", required=True, type=Path)
    status = sub.add_parser("status", help="probe a running hub over HTTP")
    status.add_argument("--data-dir", required=True, type=Path)
    status.add_argument("--url", required=True)
    status.add_argument(
        "--allowed-hosts",
        default=None,
        help="if set, probe GET /api/v1/health on loopback with Host set to the first "
        "listed hostname",
    )
    hub_user.add_parsers(sub)
    return parser


def _cmd_default_data_dir(_args: argparse.Namespace) -> int:
    print(default_hub_data_dir())
    return 0


def _cmd_argv(args: argparse.Namespace) -> int:
    print(
        shlex.join(
            hub_serve_argv(
                uv=args.uv,
                data_dir=args.data_dir,
                port=args.port,
                serve_r2=args.serve_r2,
                doppler=args.doppler,
                serve_doppler_config=args.serve_doppler_config,
            )
        )
    )
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    if args.serve_r2:
        print(
            f"[INFO] serve doppler project={CFG.DOPPLER_PROJECT} "
            f"config={args.serve_doppler_config}"
        )
    inputs = RenderInputs(
        repo_root=args.repo_root,
        uv=args.uv,
        data_dir=args.data_dir,
        port=args.port,
        backup_dest=args.backup_dest,
        keep=args.keep,
        upload_r2=args.upload_r2,
        doppler=args.doppler,
        serve_r2=args.serve_r2,
        serve_doppler_config=args.serve_doppler_config,
        allowed_hosts=args.allowed_hosts,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in render_units(args.kind, inputs).items():
        (args.out_dir / name).write_text(text, encoding="utf-8")
        print(args.out_dir / name)
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    identity = init_hub_data_dir(args.data_dir)
    print(json.dumps(asdict(identity), sort_keys=True))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    probe = probe_hub(args.url, args.data_dir)
    print(json.dumps(asdict(probe), sort_keys=True))
    print(f"[{probe.verdict}] {probe.reason}")
    worst = EXIT_BY_VERDICT[probe.verdict]
    if args.allowed_hosts:
        allow_probe = probe_allowed_host(args.url, args.allowed_hosts)
        print(json.dumps(asdict(allow_probe), sort_keys=True))
        print(f"[{allow_probe.verdict}] allowed-host: {allow_probe.reason}")
        worst = max(worst, EXIT_BY_VERDICT[allow_probe.verdict])
    return worst


#: Keys are exactly the subparser names; argparse refuses anything else.
COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "default-data-dir": _cmd_default_data_dir,
    "argv": _cmd_argv,
    "render": _cmd_render,
    "init": _cmd_init,
    "status": _cmd_status,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    commands = {**COMMANDS, **hub_user.COMMANDS}
    try:
        return commands[args.command](args)
    except (HubDeployError, machine_identity.MachineIdentityError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # ``python -m`` loads this file as ``__main__``; hub_user imports it again
    # under its package name, so run the package's copy or its HubDeployError
    # is a different class from the one ``main`` catches.
    from apps.sync_hub.hub_deploy import main as _package_main

    raise SystemExit(_package_main())
