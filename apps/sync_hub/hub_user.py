"""Per-user hubs on one host: ``hub_deploy provision-user`` and ``remove-user``.

Issue #3870. One hub holds one library (``specs/cloudsync-multi-user.md``
section 1), so every test user gets their own hub PROCESS and DATA DIR on
the shared host (section 2). This module is the one place that says what a
per-user hub is:

- data dir ``<root>/<user>/hub`` and backups ``<root>/<user>/backups``;
- a port of its own, recorded in ``<root>/hubs.json`` (allocated once, never
  handed to another user while registered);
- systemd user units ``opendj-hub-<user>.service``,
  ``opendj-hub-<user>-backup.service`` and ``opendj-hub-<user>-backup.timer``,
  rendered from the SAME templates and argv as the single hub
  (:mod:`apps.sync_hub.hub_deploy`), so a per-user unit cannot drift from what
  ``tests/cloudsync/test_hub_deploy.py`` boots;
- ``MDT_SYNC_CREDENTIAL_MODE=enforce`` (ADR-0012) and
  ``MDT_HUB_MACHINE_NAME=<user>-hub`` on the serve unit only;
- the loopback URL, plus the tailnet URL when ``--allowed-hosts`` names the
  MagicDNS host ``tailscale serve`` will forward.

It never exposes anything. ``tailscale serve`` and the ACL step are printed
as the operator's next step (runbook, "Per-user hubs"). ``--dry-run`` prints
the plan and writes nothing. Without ``--enable`` the units and the hub
identity are written and the ``systemctl`` commands are printed, so a CI box
with no user service manager proves the whole path against a temp root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from apps.shared.state import machine_identity
from apps.sync_hub import hub_deploy
from apps.sync_hub.atomic_json import write_json_atomic
from apps.sync_hub.machine_credentials import MODE_ENV as CREDENTIAL_MODE_ENV


class UCFG:
    ROOT_DIR_NAME: str = "opendj-hubs"
    REGISTRY_NAME: str = "hubs.json"
    REGISTRY_SCHEMA: int = 1
    #: Above the single hub's 8870 and below 8900; nothing else claims the window.
    PORT_BASE: int = 8871
    PORT_MAX: int = 8899
    UNIT_PREFIX: str = "opendj-hub-"
    CREDENTIAL_MODE: str = "enforce"
    #: ``apps.engine_core.__main__.HUB_MACHINE_NAME_ENV``, spelled here so this
    #: module does not import the engine; a test pins the two equal.
    MACHINE_NAME_ENV: str = "MDT_HUB_MACHINE_NAME"
    MACHINE_NAME_SUFFIX: str = "-hub"


#: A user slug: lowercase, starts with a letter, 2-32 chars. It becomes a
#: directory, a unit name and a machines.name, so nothing fancier is allowed.
USER_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
#: Template install names the single hub uses, in hub_deploy.TEMPLATE_FILES.
_SERVICE, _BACKUP_SERVICE, _BACKUP_TIMER = (
    "opendj-hub.service",
    "opendj-hub-backup.service",
    "opendj-hub-backup.timer",
)

Runner = Callable[[Sequence[str]], None]


@dataclass(frozen=True)
class UserHub:
    """One registry row: what the host knows about a user's hub."""

    user: str
    port: int
    data_dir: str
    backup_dir: str
    url: str


@dataclass(frozen=True)
class UserHubPlan:
    """Everything ``provision-user`` will do, printable before it does it."""

    hub: UserHub
    machine_name: str
    unit_dir: str
    unit_files: dict[str, str]
    serve_env: dict[str, str]
    tailnet_url: str | None
    enable_commands: list[list[str]]
    expose_commands: list[list[str]]


# ----- naming and paths ------------------------------------------------------


def validate_user(user: str) -> str:
    if not USER_RE.fullmatch(user):
        raise hub_deploy.HubDeployError(
            f"user {user!r} is not a slug: lowercase letters, digits and '-', 2-32 chars, "
            "starting with a letter"
        )
    return user


def default_root(environ: Mapping[str, str] | None = None) -> Path:
    """``$XDG_DATA_HOME/opendj-hubs`` or ``~/.local/share/opendj-hubs``."""
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home())
    xdg = env.get("XDG_DATA_HOME", "")
    return (Path(xdg) if xdg else home / ".local" / "share") / UCFG.ROOT_DIR_NAME


def default_unit_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home())
    xdg = env.get("XDG_CONFIG_HOME", "")
    return (Path(xdg) if xdg else home / ".config") / "systemd" / "user"


def unit_names(user: str) -> dict[str, str]:
    """Single-hub install name -> this user's unit name."""
    base = f"{UCFG.UNIT_PREFIX}{user}"
    return {
        _SERVICE: f"{base}.service",
        _BACKUP_SERVICE: f"{base}-backup.service",
        _BACKUP_TIMER: f"{base}-backup.timer",
    }


def machine_name(user: str) -> str:
    return f"{user}{UCFG.MACHINE_NAME_SUFFIX}"


# ----- registry --------------------------------------------------------------


def registry_path(root: Path) -> Path:
    return Path(root) / UCFG.REGISTRY_NAME


def read_registry(root: Path) -> dict[str, UserHub]:
    """``{user: UserHub}``; a missing file is an empty registry, a bad one raises."""
    path = registry_path(root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        hubs = payload.get("hubs") if payload.get("schema") == UCFG.REGISTRY_SCHEMA else None
        rows = (
            {user: UserHub(**row) for user, row in hubs.items()}
            if isinstance(hubs, dict) else None
        )
    except (OSError, ValueError, TypeError) as exc:
        raise hub_deploy.HubDeployError(f"{path} is not a usable hub registry: {exc}") from exc
    if rows is None:
        raise hub_deploy.HubDeployError(
            f"{path} is not a usable hub registry: expected schema {UCFG.REGISTRY_SCHEMA} "
            "with a hubs object"
        )
    return rows


def write_registry(root: Path, hubs: Mapping[str, UserHub]) -> Path:
    path = registry_path(root)
    payload = {
        "schema": UCFG.REGISTRY_SCHEMA,
        "hubs": {user: asdict(hub) for user, hub in sorted(hubs.items())},
    }
    write_json_atomic(path, payload)
    return path


def allocate_port(hubs: Mapping[str, UserHub], user: str, requested: int | None) -> int:
    """This user's registered port, else ``requested``, else the first free one."""
    taken = {hub.port: owner for owner, hub in hubs.items()}
    existing = hubs.get(user)
    if existing is not None:
        if requested is not None and requested != existing.port:
            raise hub_deploy.HubDeployError(
                f"{user} is already registered on port {existing.port}; remove-user first "
                f"to move it to {requested}"
            )
        return existing.port
    if requested is not None:
        if requested in taken:
            raise hub_deploy.HubDeployError(f"port {requested} belongs to {taken[requested]}")
        if not UCFG.PORT_BASE <= requested <= UCFG.PORT_MAX:
            raise hub_deploy.HubDeployError(
                f"port {requested} is outside the per-user window {UCFG.PORT_BASE}-{UCFG.PORT_MAX}"
            )
        return requested
    for port in range(UCFG.PORT_BASE, UCFG.PORT_MAX + 1):
        if port not in taken:
            return port
    raise hub_deploy.HubDeployError(
        f"no free port left in {UCFG.PORT_BASE}-{UCFG.PORT_MAX}; remove a retired user's hub"
    )


# ----- plan ------------------------------------------------------------------


def plan_user_hub(
    *,
    user: str,
    root: Path,
    port: int | None,
    unit_dir: Path,
    allowed_hosts: str | None,
    systemctl: str = "systemctl",
) -> UserHubPlan:
    validate_user(user)
    root = Path(root)
    if not root.is_absolute():
        raise hub_deploy.HubDeployError(f"--root must be absolute, got {str(root)!r}")
    data_dir = root / user / "hub"
    hub_deploy.assert_dedicated_data_dir(data_dir)
    chosen = allocate_port(read_registry(root), user, port)
    hub = UserHub(
        user=user,
        port=chosen,
        data_dir=str(data_dir),
        backup_dir=str(root / user / "backups"),
        url=f"http://{hub_deploy.CFG.HUB_HOST}:{chosen}",
    )
    names = unit_names(user)
    serve_env = hub_deploy.hub_service_env(
        allowed_hosts=allowed_hosts,
        extra_env={
            CREDENTIAL_MODE_ENV: UCFG.CREDENTIAL_MODE,
            UCFG.MACHINE_NAME_ENV: machine_name(user),
        },
    )
    first_host = (
        None if allowed_hosts is None or not allowed_hosts.strip()
        else hub_deploy._validated_allowed_hosts(allowed_hosts).split(",", 1)[0]
    )
    tailnet_url = None if first_host is None else f"https://{first_host}:{chosen}"
    return UserHubPlan(
        hub=hub,
        machine_name=machine_name(user),
        unit_dir=str(unit_dir),
        unit_files={name: str(Path(unit_dir) / name) for name in names.values()},
        serve_env=serve_env,
        tailnet_url=tailnet_url,
        enable_commands=[
            [systemctl, "--user", "daemon-reload"],
            [systemctl, "--user", "enable", "--now", names[_SERVICE], names[_BACKUP_TIMER]],
            # `enable --now` only STARTS an inactive unit; an already-active
            # one (a reprovision with changed settings, e.g. --allowed-hosts)
            # keeps running with its OLD environment. `restart` picks up the
            # rewritten unit file either way -- starting a stopped unit or
            # cycling a live one (Codex P1, PR #3879).
            [systemctl, "--user", "restart", names[_SERVICE]],
        ],
        expose_commands=[
            ["tailscale", "serve", "--bg", f"--https={chosen}", hub.url],
        ],
    )


# ----- provision / remove ----------------------------------------------------


def _run_checked(argv: Sequence[str]) -> None:
    subprocess.run(list(argv), check=True)


def provision_user_hub(
    plan: UserHubPlan,
    *,
    repo_root: Path,
    uv: str,
    keep: int,
    allowed_hosts: str | None,
    enable: bool,
    run: Runner = _run_checked,
) -> machine_identity.MachineIdentity:
    """Create the data dir, register the hub identity, write units and the registry."""
    data_dir = Path(plan.hub.data_dir)
    backup_dir = Path(plan.hub.backup_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = hub_deploy.init_hub_data_dir(
        data_dir, env={machine_identity.IS_HUB_ENV: "1"}, name=plan.machine_name
    )
    rendered = hub_deploy.render_units(
        "systemd",
        hub_deploy.RenderInputs(
            repo_root=Path(repo_root),
            uv=uv,
            data_dir=data_dir,
            port=plan.hub.port,
            backup_dest=backup_dir,
            keep=keep,
            upload_r2=False,
            doppler=None,
            allowed_hosts=allowed_hosts,
            extra_env={
                CREDENTIAL_MODE_ENV: UCFG.CREDENTIAL_MODE,
                UCFG.MACHINE_NAME_ENV: plan.machine_name,
            },
        ),
    )
    names = unit_names(plan.hub.user)
    Path(plan.unit_dir).mkdir(parents=True, exist_ok=True)
    for install_name, text in rendered.items():
        Path(plan.unit_files[names[install_name]]).write_text(text, encoding="utf-8")
    root = data_dir.parents[1]
    hubs = dict(read_registry(root))
    hubs[plan.hub.user] = plan.hub
    write_registry(root, hubs)
    if enable:
        for command in plan.enable_commands:
            run(command)
    return identity


@dataclass(frozen=True)
class UserHubRemoval:
    hub: UserHub
    removed_units: list[str]
    disable_commands: list[list[str]]
    kept: list[str]


def remove_user_hub(
    *,
    user: str,
    root: Path,
    unit_dir: Path,
    systemctl: str = "systemctl",
    run: Runner = _run_checked,
) -> UserHubRemoval:
    """Stop and disable the units, delete them, drop the registry row. Data stays."""
    validate_user(user)
    hubs = dict(read_registry(root))
    hub = hubs.get(user)
    if hub is None:
        raise hub_deploy.HubDeployError(f"{user} has no hub registered in {registry_path(root)}")
    names = unit_names(user)
    present = [name for name in names.values() if (Path(unit_dir) / name).exists()]
    commands: list[list[str]] = []
    if present:
        commands.append([systemctl, "--user", "disable", "--now", *present])
    for command in commands:
        run(command)
    for name in present:
        (Path(unit_dir) / name).unlink()
    if present:
        reload = [systemctl, "--user", "daemon-reload"]
        run(reload)
        commands.append(reload)
    del hubs[user]
    write_registry(root, hubs)
    return UserHubRemoval(
        hub=hub,
        removed_units=[str(Path(unit_dir) / name) for name in present],
        disable_commands=commands,
        kept=[hub.data_dir, hub.backup_dir],
    )


# ----- CLI -------------------------------------------------------------------


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    provision = sub.add_parser(
        "provision-user", help="create one test user's hub: data dir, port, units, enforce"
    )
    provision.add_argument("--user", required=True, help="user slug, e.g. ben")
    provision.add_argument("--uv", required=True, help="absolute path to uv")
    provision.add_argument("--repo-root", required=True, type=Path)
    provision.add_argument("--root", type=Path, default=None, help="hubs root (default: XDG data)")
    provision.add_argument("--port", type=int, default=None, help="default: next free in window")
    provision.add_argument("--unit-dir", type=Path, default=None)
    provision.add_argument("--keep", type=int, default=hub_deploy.CFG.BACKUP_KEEP)
    provision.add_argument(
        "--allowed-hosts", default=None, help="MagicDNS host(s) for tailscale serve"
    )
    provision.add_argument("--systemctl", default="systemctl")
    provision.add_argument("--enable", action="store_true", help="run systemctl enable --now")
    provision.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    remove = sub.add_parser("remove-user", help="disable and delete one user's hub units")
    remove.add_argument("--user", required=True)
    remove.add_argument("--root", type=Path, default=None)
    remove.add_argument("--unit-dir", type=Path, default=None)
    remove.add_argument("--systemctl", default="systemctl")


def _print_next_steps(plan: UserHubPlan, *, enabled: bool) -> None:
    print(f"[OK] hub url (loopback): {plan.hub.url}")
    if plan.tailnet_url is not None:
        print(f"[OK] hub url (tailnet, after tailscale serve): {plan.tailnet_url}")
    else:
        print("[WARN] no --allowed-hosts: tailnet requests will answer 403 HOST_NOT_ALLOWED")
    if not enabled:
        print("[NEXT] enable the units:")
        for command in plan.enable_commands:
            print(f"       {shlex.join(command)}")
    print("[NEXT] expose on the tailnet ONLY after the ACL step (runbook, Per-user hubs):")
    for command in plan.expose_commands:
        print(f"       {shlex.join(command)}")


def cmd_provision_user(args: argparse.Namespace) -> int:
    plan = plan_user_hub(
        user=args.user,
        root=args.root if args.root is not None else default_root(),
        port=args.port,
        unit_dir=args.unit_dir if args.unit_dir is not None else default_unit_dir(),
        allowed_hosts=args.allowed_hosts,
        systemctl=args.systemctl,
    )
    print(json.dumps(asdict(plan), sort_keys=True))
    if args.dry_run:
        print("[DRY-RUN] nothing written")
        return 0
    identity = provision_user_hub(
        plan,
        repo_root=args.repo_root,
        uv=args.uv,
        keep=args.keep,
        allowed_hosts=args.allowed_hosts,
        enable=args.enable,
    )
    print(f"[OK] hub machine {identity.machine_id} registered as {identity.name}")
    for path in plan.unit_files.values():
        print(f"[OK] wrote {path}")
    _print_next_steps(plan, enabled=args.enable)
    return 0


def cmd_remove_user(args: argparse.Namespace) -> int:
    removal = remove_user_hub(
        user=args.user,
        root=args.root if args.root is not None else default_root(),
        unit_dir=args.unit_dir if args.unit_dir is not None else default_unit_dir(),
        systemctl=args.systemctl,
    )
    print(json.dumps(asdict(removal), sort_keys=True))
    for kept in removal.kept:
        print(f"[OK] kept (archive or delete by hand): {kept}")
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "provision-user": cmd_provision_user,
    "remove-user": cmd_remove_user,
}

__all__: list[str] = [
    "COMMANDS",
    "UCFG",
    "USER_RE",
    "UserHub",
    "UserHubPlan",
    "UserHubRemoval",
    "add_parsers",
    "allocate_port",
    "default_root",
    "default_unit_dir",
    "machine_name",
    "plan_user_hub",
    "provision_user_hub",
    "read_registry",
    "remove_user_hub",
    "unit_names",
    "validate_user",
    "write_registry",
]
