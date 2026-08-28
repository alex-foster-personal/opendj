"""Worktree-local Web UI port configuration and reservation manager.

Requirements:

- ✔︎ One ignored root ``.env`` owns the backend and frontend port values.
- ✔︎ The API proxy target is derived from the backend port.
- ✔︎ A Git-common-dir registry prevents copied worktrees from sharing ports.
- ✔︎ Port availability checks are per service and use cross-platform sockets.
- ✔︎ CLI values override process environment, which overrides root ``.env``.

Acceptance tests:

- [if] two worktrees claim a copied ``.env`` [then ⛔️] they receive one pair.
- [if] an unrelated backend owns the reserved port [then ⛔️] Vite starts.
- [if] a port is missing, malformed, privileged, or out of range [then ⛔️]
  startup continues.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

BACKEND_ENV = "MUSIC_DJ_BACKEND_PORT"
FRONTEND_ENV = "MUSIC_DJ_FRONTEND_PORT"
OBSOLETE_PROXY_ENV = "MUSIC_DJ_API_PROXY_TARGET"
MIN_PORT = 1024
MAX_PORT = 65535
POOL_SIZE = 120
LOCK_TIMEOUT_SECONDS = 2.0
BACKEND_POOL_START = 8680
FRONTEND_POOL_START = 9400
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEBUI_ENV_FILE = PROJECT_ROOT / ".env"


class PortConfigError(ValueError):
    """The worktree port contract is missing, invalid, or unsafe."""


@dataclass(frozen=True)
class WebuiPorts:
    """Validated backend and frontend ports for one worktree."""

    backend: int
    frontend: int

    def __post_init__(self) -> None:
        _validate_port(BACKEND_ENV, self.backend)
        _validate_port(FRONTEND_ENV, self.frontend)
        if self.backend == self.frontend:
            raise PortConfigError("backend and frontend ports must be different")

    @property
    def api_proxy_target(self) -> str:
        return f"http://127.0.0.1:{self.backend}"


def _validate_port(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PortConfigError(f"{name} must be an integer")
    if value < MIN_PORT or value > MAX_PORT:
        raise PortConfigError(
            f"{name} must be between {MIN_PORT} and {MAX_PORT}, got {value}"
        )
    return value


def _parse_port(name: str, raw_value: str | int | None) -> int:
    if raw_value is None or str(raw_value).strip() == "":
        raise PortConfigError(f"{name} is required")
    value_text = str(raw_value).strip()
    if not value_text.isascii() or not value_text.isdecimal():
        raise PortConfigError(f"{name} must be an integer, got {value_text!r}")
    return _validate_port(name, int(value_text))


def _read_dotenv(dotenv_path: Path) -> dict[str, str]:
    if not dotenv_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.removeprefix("export ").strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def _resolved_raw_value(
    name: str,
    environ: Mapping[str, str],
    dotenv_values: Mapping[str, str],
) -> str | None:
    if name in environ:
        return environ[name]
    return dotenv_values.get(name)


def resolve_ports(
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = WEBUI_ENV_FILE,
) -> WebuiPorts:
    """Resolve both ports with process environment above root ``.env``."""
    effective_environ = os.environ if environ is None else environ
    dotenv_values = _read_dotenv(dotenv_path)
    return WebuiPorts(
        backend=_parse_port(
            BACKEND_ENV,
            _resolved_raw_value(BACKEND_ENV, effective_environ, dotenv_values),
        ),
        frontend=_parse_port(
            FRONTEND_ENV,
            _resolved_raw_value(FRONTEND_ENV, effective_environ, dotenv_values),
        ),
    )


def resolve_backend_port(
    explicit_port: int | str | None,
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = WEBUI_ENV_FILE,
) -> int:
    """Resolve CLI > process environment > root ``.env`` for the daemon."""
    if explicit_port is not None:
        return _parse_port("--port", explicit_port)
    effective_environ = os.environ if environ is None else environ
    dotenv_values = _read_dotenv(dotenv_path)
    return _parse_port(
        BACKEND_ENV,
        _resolved_raw_value(BACKEND_ENV, effective_environ, dotenv_values),
    )


def resolve_frontend_port(
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = WEBUI_ENV_FILE,
) -> int:
    """Resolve the frontend port from process environment or root ``.env``."""
    effective_environ = os.environ if environ is None else environ
    dotenv_values = _read_dotenv(dotenv_path)
    return _parse_port(
        FRONTEND_ENV,
        _resolved_raw_value(FRONTEND_ENV, effective_environ, dotenv_values),
    )


def _write_dotenv_ports(dotenv_path: Path, ports: WebuiPorts) -> None:
    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = (
        dotenv_path.read_text(encoding="utf-8").splitlines()
        if dotenv_path.is_file()
        else []
    )
    replacements = {
        BACKEND_ENV: str(ports.backend),
        FRONTEND_ENV: str(ports.frontend),
    }
    seen: set[str] = set()
    output_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        name = stripped.split("=", 1)[0].removeprefix("export ").strip()
        if name == OBSOLETE_PROXY_ENV and "=" in stripped:
            continue
        if name in replacements and "=" in stripped:
            output_lines.append(f"{name}={replacements[name]}")
            seen.add(name)
        else:
            output_lines.append(line)
    for name, value in replacements.items():
        if name not in seen:
            output_lines.append(f"{name}={value}")
    content = "\n".join(output_lines).rstrip() + "\n"
    # Skip the write when nothing changes: vite watches the root .env and
    # re-runs claim on every config load, so an unconditional rewrite feeds
    # its own change event and wedges the dev server in a restart loop.
    if dotenv_path.is_file() and dotenv_path.read_text(encoding="utf-8") == content:
        return
    _atomic_write_text(dotenv_path, content)


def _atomic_write_text(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temp_file:
        temp_file.write(content)
        temp_path = Path(temp_file.name)
    try:
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _git_path(argument: str) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", argument],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def _repo_root(repo_root: Path | None) -> Path:
    return (repo_root or _git_path("--show-toplevel")).resolve()


def _common_dir(common_dir: Path | None) -> Path:
    return (common_dir or _git_path("--git-common-dir")).resolve()


def _registry_path(common_dir: Path) -> Path:
    return common_dir / "music-dj-tools" / "worktree-ports.json"


@contextmanager
def _locked_registry(common_dir: Path) -> Iterator[Path]:
    registry_path = _registry_path(common_dir)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_dir = registry_path.parent / ".worktree-ports.lock"
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise PortConfigError(
                    f"port registry stayed locked at {lock_dir} for "
                    f"{LOCK_TIMEOUT_SECONDS:.1f}s"
                ) from exc
            time.sleep(0.05)
    try:
        yield registry_path
    finally:
        lock_dir.rmdir()


def _load_registry(registry_path: Path) -> dict[str, WebuiPorts]:
    if not registry_path.is_file():
        return {}
    try:
        raw_registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise PortConfigError(
            f"invalid port registry at {registry_path}: {exc}"
        ) from exc
    if not isinstance(raw_registry, dict):
        raise PortConfigError(f"invalid port registry object at {registry_path}")
    if raw_registry.get("schema") != 1:
        raise PortConfigError(f"unsupported port registry schema at {registry_path}")
    raw_reservations = raw_registry.get("reservations")
    if not isinstance(raw_reservations, dict):
        raise PortConfigError(f"invalid reservations in {registry_path}")
    reservations: dict[str, WebuiPorts] = {}
    claimed_ports: dict[int, str] = {}
    for path, raw_ports in raw_reservations.items():
        if not isinstance(path, str) or not isinstance(raw_ports, dict):
            raise PortConfigError(f"invalid reservation entry in {registry_path}")
        ports = WebuiPorts(
            backend=_parse_port("registry backend", raw_ports.get("backend")),
            frontend=_parse_port("registry frontend", raw_ports.get("frontend")),
        )
        for port in (ports.backend, ports.frontend):
            existing_path = claimed_ports.get(port)
            if existing_path is not None:
                raise PortConfigError(
                    f"registry port {port} is claimed by both {existing_path} and {path}"
                )
            claimed_ports[port] = path
        reservations[path] = ports
    return reservations


def _write_registry(
    registry_path: Path,
    reservations: Mapping[str, WebuiPorts],
) -> None:
    payload = {
        "schema": 1,
        "reservations": {
            path: {"backend": ports.backend, "frontend": ports.frontend}
            for path, ports in sorted(reservations.items())
        },
    }
    _atomic_write_text(registry_path, json.dumps(payload, indent=2) + "\n")


def _prune_missing_worktrees(
    reservations: Mapping[str, WebuiPorts],
) -> dict[str, WebuiPorts]:
    return {path: ports for path, ports in reservations.items() if Path(path).is_dir()}


def _port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Match the actual uvicorn/Vite listener behavior. Without this a
        # clean restart is falsely blocked by the old socket's TIME_WAIT.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _pair_is_available(ports: WebuiPorts) -> bool:
    return _port_is_available(ports.backend) and _port_is_available(ports.frontend)


def _backend_listener_matches_pair(ports: WebuiPorts) -> bool:
    """Return whether the occupied backend exposes this pair's diagnostics."""
    url = f"{ports.api_proxy_target}/api/v1/settings"
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        return False
    items = {
        item.get("key"): item.get("value")
        for group in payload["groups"]
        if isinstance(group, dict) and isinstance(group.get("items"), list)
        for item in group["items"]
        if isinstance(item, dict)
    }
    expected_origin = f"http://127.0.0.1:{ports.frontend}"
    return (
        items.get("port") == ports.backend
        and isinstance(items.get("cors_allow_origins"), list)
        and expected_origin in items["cors_allow_origins"]
    )


def _pair_owner(
    ports: WebuiPorts,
    reservations: Mapping[str, WebuiPorts],
) -> str | None:
    for path, reserved_ports in reservations.items():
        if ports == reserved_ports:
            return path
    return None


def _pair_overlaps_reservation(
    ports: WebuiPorts,
    reservations: Mapping[str, WebuiPorts],
    *,
    excluding_path: str,
) -> bool:
    candidate_ports = {ports.backend, ports.frontend}
    return any(
        path != excluding_path
        and bool(candidate_ports & {reserved.backend, reserved.frontend})
        for path, reserved in reservations.items()
    )


def _configured_candidate(
    dotenv_path: Path,
    environ: Mapping[str, str],
) -> WebuiPorts | None:
    dotenv_values = _read_dotenv(dotenv_path)
    backend_raw = _resolved_raw_value(BACKEND_ENV, environ, dotenv_values)
    frontend_raw = _resolved_raw_value(FRONTEND_ENV, environ, dotenv_values)
    backend_missing = backend_raw is None or backend_raw.strip() == ""
    frontend_missing = frontend_raw is None or frontend_raw.strip() == ""
    if backend_missing and frontend_missing:
        return None
    if backend_missing or frontend_missing:
        missing_name = BACKEND_ENV if backend_missing else FRONTEND_ENV
        raise PortConfigError(f"{missing_name} is required when claiming a port pair")
    return WebuiPorts(
        backend=_parse_port(BACKEND_ENV, backend_raw),
        frontend=_parse_port(FRONTEND_ENV, frontend_raw),
    )


def _allocate_pair(reservations: Mapping[str, WebuiPorts]) -> WebuiPorts:
    reserved_ports = {
        port
        for ports in reservations.values()
        for port in (ports.backend, ports.frontend)
    }
    for slot in range(POOL_SIZE):
        candidate = WebuiPorts(
            backend=BACKEND_POOL_START + slot,
            frontend=FRONTEND_POOL_START + slot,
        )
        if candidate.backend in reserved_ports or candidate.frontend in reserved_ports:
            continue
        if _pair_is_available(candidate):
            return candidate
    raise PortConfigError(
        "no free worktree port pair remains; release stale reservations first"
    )


def claim_ports(
    *,
    repo_root: Path | None = None,
    common_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    requested: WebuiPorts | None = None,
) -> WebuiPorts:
    """Claim or restore a unique pair and rewrite the worktree root ``.env``."""
    resolved_root = _repo_root(repo_root)
    resolved_common_dir = _common_dir(common_dir)
    effective_environ = os.environ if environ is None else environ
    dotenv_path = resolved_root / ".env"
    configured = requested or _configured_candidate(dotenv_path, effective_environ)
    root_key = str(resolved_root)

    with _locked_registry(resolved_common_dir) as registry_path:
        reservations = _prune_missing_worktrees(_load_registry(registry_path))
        previous = dict(reservations)
        current = reservations.get(root_key)
        if current is not None and (configured is None or configured == current):
            selected = current
        elif (
            configured is not None
            and not _pair_overlaps_reservation(
                configured,
                reservations,
                excluding_path=root_key,
            )
            and _pair_is_available(configured)
        ):
            selected = configured
        else:
            reservations.pop(root_key, None)
            selected = _allocate_pair(reservations)

        reservations[root_key] = selected
        _write_registry(registry_path, reservations)
        try:
            _write_dotenv_ports(dotenv_path, selected)
        except BaseException:
            _write_registry(registry_path, previous)
            raise
    return selected


def _require_reservation(
    repo_root: Path,
    common_dir: Path,
    environ: Mapping[str, str],
) -> WebuiPorts:
    ports = resolve_ports(environ=environ, dotenv_path=repo_root / ".env")
    with _locked_registry(common_dir) as registry_path:
        reservations = _prune_missing_worktrees(_load_registry(registry_path))
        owner = _pair_owner(ports, reservations)
        if reservations.get(str(repo_root)) != ports:
            owner_note = f"; currently owned by {owner}" if owner is not None else ""
            raise PortConfigError(
                f"port pair {ports.backend}/{ports.frontend} is not reserved by "
                f"this worktree{owner_note}; run `just webui-ports-claim`"
            )
    return ports


def check_reservation(
    service: str,
    *,
    repo_root: Path | None = None,
    common_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> WebuiPorts:
    """Verify registry ownership and availability for one service or both."""
    if service not in {"backend", "frontend", "all"}:
        raise PortConfigError(f"unknown service {service!r}")
    resolved_root = _repo_root(repo_root)
    resolved_common_dir = _common_dir(common_dir)
    effective_environ = os.environ if environ is None else environ
    ports = _require_reservation(
        resolved_root,
        resolved_common_dir,
        effective_environ,
    )
    checks = [("backend", ports.backend), ("frontend", ports.frontend)]
    if service != "all":
        checks = [(service, getattr(ports, service))]
    for service_name, port in checks:
        if not _port_is_available(port):
            raise PortConfigError(f"{service_name} port {port} is already in use")
    if (
        service == "frontend"
        and not _port_is_available(ports.backend)
        and not _backend_listener_matches_pair(ports)
    ):
        raise PortConfigError(
            f"backend port {ports.backend} is occupied by a process that does not "
            "match this worktree's reserved pair"
        )
    return ports


def release_ports(
    *,
    repo_root: Path | None = None,
    common_dir: Path | None = None,
) -> bool:
    """Release the current worktree's pair without deleting its ``.env``."""
    resolved_root = _repo_root(repo_root)
    resolved_common_dir = _common_dir(common_dir)
    with _locked_registry(resolved_common_dir) as registry_path:
        reservations = _prune_missing_worktrees(_load_registry(registry_path))
        removed = reservations.pop(str(resolved_root), None) is not None
        _write_registry(registry_path, reservations)
    return removed


def _print_ports(ports: WebuiPorts) -> None:
    print(f"backend  http://127.0.0.1:{ports.backend}")
    print(f"frontend http://127.0.0.1:{ports.frontend}")
    print(f"proxy    {ports.api_proxy_target}")


def _print_ports_json(ports: WebuiPorts) -> None:
    print(json.dumps({
        "backend": ports.backend,
        "frontend": ports.frontend,
        "api_proxy_target": ports.api_proxy_target,
    }))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.webui.port_config",
        description="claim and validate worktree-local Web UI ports",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    claim_parser = commands.add_parser("claim", help="claim or restore a unique pair")
    claim_parser.add_argument("--backend-port", type=int)
    claim_parser.add_argument("--frontend-port", type=int)
    claim_parser.add_argument("--json", action="store_true")
    check_parser = commands.add_parser("check", help="check ownership and availability")
    check_parser.add_argument(
        "--service",
        choices=("backend", "frontend", "all"),
        default="all",
    )
    check_parser.add_argument("--json", action="store_true")
    show_parser = commands.add_parser("show", help="show the reserved pair")
    show_parser.add_argument("--json", action="store_true")
    commands.add_parser("release", help="release the current worktree's pair")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "claim":
            if (args.backend_port is None) != (args.frontend_port is None):
                parser.error(
                    "--backend-port and --frontend-port must be supplied together"
                )
            requested = (
                WebuiPorts(args.backend_port, args.frontend_port)
                if args.backend_port is not None
                else None
            )
            ports = claim_ports(requested=requested)
            if args.json:
                _print_ports_json(ports)
            else:
                print("[OK] Reserved worktree ports")
                _print_ports(ports)
        elif args.command == "check":
            ports = check_reservation(args.service)
            if args.json:
                _print_ports_json(ports)
            else:
                print(f"[OK] {args.service} port reservation is free")
                _print_ports(ports)
        elif args.command == "show":
            ports = _require_reservation(
                _repo_root(None),
                _common_dir(None),
                os.environ,
            )
            if args.json:
                _print_ports_json(ports)
            else:
                _print_ports(ports)
        elif args.command == "release":
            released = release_ports()
            print("[OK] Released worktree ports" if released else "[OK] No reservation")
        else:
            parser.error(f"unhandled command {args.command!r}")
    except PortConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PortConfigError",
    "WebuiPorts",
    "check_reservation",
    "claim_ports",
    "main",
    "release_ports",
    "resolve_backend_port",
    "resolve_frontend_port",
    "resolve_ports",
]
