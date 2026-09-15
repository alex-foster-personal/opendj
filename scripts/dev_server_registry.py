"""Central dev-server registry and generated Claude launch configuration.

Requirements (issue #2558):
- [if] `.claude/dev-servers.json` changes [then] `.claude/launch.json` is
  regenerated from it instead of hand-edited, [else stop]
- [if] CI or `just launch-json-check` runs on a clean checkout [then] the
  committed launch file matches the registry's default Web UI port profile,
  [else stop]
- [if] `just launch-json` runs in a worktree with a claimed pair [then] the
  generated Web UI ports come from `apps.webui.port_config`, [else stop]
- [if] the registry is malformed or two servers resolve to the same port
  [then] generation fails loudly, [else stop]

Acceptance tests:
- [if] `python -m scripts.dev_server_registry --check --profile default`
  runs on a clean tree [then] it exits 0 when launch.json matches, [else stop]
- [if] a disposable registry has duplicate resolved ports [then] generation
  raises before writing output, [else stop]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apps.webui.port_config import PortConfigError, resolve_ports

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = ROOT / ".claude" / "dev-servers.json"
DEFAULT_LAUNCH_PATH = ROOT / ".claude" / "launch.json"
LAUNCH_VERSION = "0.0.1"
Profile = Literal["default", "worktree"]
PORT_KINDS = frozenset({"fixed", "worktree"})
WORKTREE_SERVICES = frozenset({"backend", "frontend"})


class DevServerRegistryError(ValueError):
    """The dev-server registry or generated launch output is invalid."""


@dataclass(frozen=True)
class ResolvedServer:
    name: str
    runtime_executable: str
    runtime_args: tuple[str, ...]
    port: int


# ----- registry loading ---------------------------------------------------


def _load_registry_text(text: str, *, source: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DevServerRegistryError(f"invalid JSON in {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DevServerRegistryError(f"{source} must be a JSON object")
    if payload.get("schemaVersion") != 1:
        raise DevServerRegistryError(
            f"{source} has unsupported schemaVersion {payload.get('schemaVersion')!r}; expected 1"
        )
    servers = payload.get("servers")
    if not isinstance(servers, list) or not servers:
        raise DevServerRegistryError(f"{source} must contain a non-empty servers array")
    default_ports = payload.get("defaultWebuiPorts")
    if not isinstance(default_ports, dict):
        raise DevServerRegistryError(f"{source} must contain defaultWebuiPorts")
    for service in WORKTREE_SERVICES:
        raw_value = default_ports.get(service)
        if not isinstance(raw_value, int) or isinstance(raw_value, bool):
            raise DevServerRegistryError(
                f"{source} defaultWebuiPorts.{service} must be an integer"
            )
        _validate_port(f"defaultWebuiPorts.{service}", raw_value)
    return payload


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    if not path.is_file():
        raise DevServerRegistryError(f"registry not found at {path}")
    return _load_registry_text(path.read_text(encoding="utf-8"), source=str(path))


# ----- validation and resolution ------------------------------------------


def _validate_port(label: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DevServerRegistryError(f"{label} must be an integer")
    if value < 1024 or value > 65535:
        raise DevServerRegistryError(f"{label} must be between 1024 and 65535, got {value}")
    return value


def _validate_owner(owner: Any, *, source: str, server_name: str) -> None:
    if not isinstance(owner, dict):
        raise DevServerRegistryError(f"{source} server {server_name!r} owner must be an object")
    host = owner.get("host")
    scope = owner.get("scope")
    worktree = owner.get("worktree")
    if not isinstance(host, str) or not host.strip():
        raise DevServerRegistryError(
            f"{source} server {server_name!r} owner.host must be a non-empty string"
        )
    has_scope = isinstance(scope, str) and scope.strip()
    has_worktree = isinstance(worktree, str) and worktree.strip()
    if not has_scope and not has_worktree:
        raise DevServerRegistryError(
            f"{source} server {server_name!r} owner must include scope or worktree identity"
        )


def _resolve_port_declaration(
    port_decl: Any,
    *,
    source: str,
    server_name: str,
    profile: Profile,
    registry: Mapping[str, Any],
    dotenv_path: Path,
) -> int:
    if not isinstance(port_decl, dict):
        raise DevServerRegistryError(
            f"{source} server {server_name!r} port must be an object"
        )
    kind = port_decl.get("kind")
    if kind not in PORT_KINDS:
        raise DevServerRegistryError(
            f"{source} server {server_name!r} has unsupported port kind {kind!r}"
        )
    if kind == "fixed":
        value = port_decl.get("value")
        if not isinstance(value, int) or isinstance(value, bool):
            raise DevServerRegistryError(
                f"{source} server {server_name!r} fixed port value must be an integer"
            )
        return _validate_port(f"{server_name}.port", value)
    service = port_decl.get("service")
    if service not in WORKTREE_SERVICES:
        raise DevServerRegistryError(
            f"{source} server {server_name!r} worktree port service must be "
            f"one of {sorted(WORKTREE_SERVICES)}, got {service!r}"
        )
    if profile == "default":
        default_ports = registry["defaultWebuiPorts"]
        return _validate_port(
            f"defaultWebuiPorts.{service}",
            int(default_ports[service]),
        )
    try:
        ports = resolve_ports(dotenv_path=dotenv_path)
    except PortConfigError as exc:
        raise DevServerRegistryError(
            f"worktree profile cannot resolve {server_name!r} {service} port from "
            f"{dotenv_path}: {exc}"
        ) from exc
    return ports.backend if service == "backend" else ports.frontend


def _validate_server_record(
    record: Any,
    *,
    source: str,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise DevServerRegistryError(f"{source} contains a non-object server record")
    name = record.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DevServerRegistryError(f"{source} server name must be a non-empty string")
    runtime_executable = record.get("runtimeExecutable")
    if not isinstance(runtime_executable, str) or not runtime_executable.strip():
        raise DevServerRegistryError(
            f"{source} server {name!r} runtimeExecutable must be a non-empty string"
        )
    runtime_args = record.get("runtimeArgs")
    if not isinstance(runtime_args, list) or not runtime_args:
        raise DevServerRegistryError(
            f"{source} server {name!r} runtimeArgs must be a non-empty array"
        )
    if not all(isinstance(arg, str) for arg in runtime_args):
        raise DevServerRegistryError(
            f"{source} server {name!r} runtimeArgs must contain only strings"
        )
    _validate_owner(record.get("owner"), source=source, server_name=name)
    if "port" not in record:
        raise DevServerRegistryError(f"{source} server {name!r} is missing port")
    return record


def resolve_servers(
    registry: Mapping[str, Any],
    *,
    profile: Profile,
    dotenv_path: Path,
    source: str,
) -> list[ResolvedServer]:
    seen_names: set[str] = set()
    resolved_ports: dict[int, str] = {}
    resolved: list[ResolvedServer] = []
    for raw_record in registry["servers"]:
        record = _validate_server_record(raw_record, source=source)
        name = record["name"]
        if name in seen_names:
            raise DevServerRegistryError(f"{source} contains duplicate server name {name!r}")
        seen_names.add(name)
        port = _resolve_port_declaration(
            record["port"],
            source=source,
            server_name=name,
            profile=profile,
            registry=registry,
            dotenv_path=dotenv_path,
        )
        existing_name = resolved_ports.get(port)
        if existing_name is not None:
            raise DevServerRegistryError(
                f"{source} servers {existing_name!r} and {name!r} both resolve to port {port}"
            )
        resolved_ports[port] = name
        resolved.append(
            ResolvedServer(
                name=name,
                runtime_executable=record["runtimeExecutable"],
                runtime_args=tuple(record["runtimeArgs"]),
                port=port,
            )
        )
    return resolved


# ----- launch generation --------------------------------------------------


def build_launch_document(servers: Sequence[ResolvedServer]) -> dict[str, Any]:
    return {
        "version": LAUNCH_VERSION,
        "configurations": [
            {
                "name": server.name,
                "runtimeExecutable": server.runtime_executable,
                "runtimeArgs": list(server.runtime_args),
                "port": server.port,
            }
            for server in servers
        ],
    }


def render_launch_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


def generate_launch_text(
    *,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    profile: Profile = "worktree",
    dotenv_path: Path | None = None,
) -> str:
    registry = load_registry(registry_path)
    resolved_dotenv = dotenv_path if dotenv_path is not None else ROOT / ".env"
    servers = resolve_servers(
        registry,
        profile=profile,
        dotenv_path=resolved_dotenv,
        source=str(registry_path),
    )
    return render_launch_json(build_launch_document(servers))


def write_launch_json(
    *,
    launch_path: Path = DEFAULT_LAUNCH_PATH,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    profile: Profile = "worktree",
    dotenv_path: Path | None = None,
) -> str:
    content = generate_launch_text(
        registry_path=registry_path,
        profile=profile,
        dotenv_path=dotenv_path,
    )
    launch_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=launch_path.parent,
        prefix=f".{launch_path.name}.",
        delete=False,
    ) as temp_file:
        temp_file.write(content)
        temp_path = Path(temp_file.name)
    try:
        temp_path.replace(launch_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return content


def check_launch_json(
    *,
    launch_path: Path = DEFAULT_LAUNCH_PATH,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    profile: Profile = "default",
    dotenv_path: Path | None = None,
) -> None:
    if not launch_path.is_file():
        raise DevServerRegistryError(
            f"missing generated launch file at {launch_path}; "
            f"run `just launch-json --profile {profile}`"
        )
    expected = generate_launch_text(
        registry_path=registry_path,
        profile=profile,
        dotenv_path=dotenv_path,
    )
    actual = launch_path.read_text(encoding="utf-8")
    if actual != expected:
        raise DevServerRegistryError(
            f"{launch_path} is stale for profile {profile!r}. "
            f"Refresh from {registry_path} with "
            f"`just launch-json --profile {profile}` or "
            f"`uv run --no-sync python -m scripts.dev_server_registry --write --profile {profile}`"
        )


# ----- CLI ----------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.dev_server_registry",
        description="generate or check .claude/launch.json from .claude/dev-servers.json",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
        help="path to the dev-server registry JSON",
    )
    parser.add_argument(
        "--launch",
        type=Path,
        default=DEFAULT_LAUNCH_PATH,
        help="path to the generated launch.json",
    )
    parser.add_argument(
        "--dotenv",
        type=Path,
        default=None,
        help="optional root .env for worktree profile resolution",
    )
    parser.add_argument(
        "--profile",
        choices=("default", "worktree"),
        default="worktree",
        help="default uses registry defaultWebuiPorts; worktree reads .env via port_config",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--write",
        action="store_true",
        help="write the generated launch.json",
    )
    action.add_argument(
        "--check",
        action="store_true",
        help="fail when the launch file differs from the generated output",
    )
    action.add_argument(
        "--print",
        action="store_true",
        help="print the generated launch.json to stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.write:
            write_launch_json(
                launch_path=args.launch,
                registry_path=args.registry,
                profile=args.profile,
                dotenv_path=args.dotenv,
            )
            print(f"[OK] wrote {args.launch} from {args.registry} (profile={args.profile})")
        elif args.check:
            check_launch_json(
                launch_path=args.launch,
                registry_path=args.registry,
                profile=args.profile,
                dotenv_path=args.dotenv,
            )
            print(
                f"[OK] {args.launch} matches {args.registry} (profile={args.profile})"
            )
        else:
            sys.stdout.write(
                generate_launch_text(
                    registry_path=args.registry,
                    profile=args.profile,
                    dotenv_path=args.dotenv,
                )
            )
    except DevServerRegistryError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
