"""Restart the agentbox web UI behind tailscale serve and probe it.

On hostname ``agentbox`` this is local. On a Mac, ``just run-agentbox``
BatchMode-SSHs to the tailnet alias ``agentbox`` and runs ``--local`` there.
It does not bind, serve, or rewrite Tailscale config on the caller.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path

from apps.webui.port_config import (
    PROJECT_ROOT,
    WEBUI_ENV_FILE,
    PortConfigError,
    WebuiPorts,
    _read_dotenv,
    _resolved_raw_value,
    claim_ports,
)

ALLOWED_HOSTS_ENV = "MUSIC_DJ_ALLOWED_HOSTS"
SERVE_HTTPS_PORT = 443
LEGACY_SERVE_HTTP_PORT = 8080
BACKEND_WAIT_SECONDS = 30.0
FRONTEND_WAIT_SECONDS = 45.0
STOP_WAIT_SECONDS = 5.0
PID_RE = re.compile(r"pid=(\d+)")
LOG_DIR = Path.home() / ".local/share/music-dj-tools/webui"
SCRATCH_DIR = PROJECT_ROOT / ".tmp"
EPHEMERAL_LOG_DIR = Path(tempfile.gettempdir()) / "music-dj-tools"
FRONTEND_DIR = PROJECT_ROOT / "apps" / "webui" / "frontend"
AGENTBOX_HOSTNAME = "agentbox"
ALLOWED_SSH_HOSTS = frozenset({"agentbox", "agentbox.example-tailnet.ts.net"})
SSH_HOST = "agentbox"
REMOTE_REPO = "/root/music-dj-tools"
SSH_CONNECT_TIMEOUT = "8"


def resolve_allowed_hosts(
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = WEBUI_ENV_FILE,
) -> list[str]:
    """Bare hostnames from shell or root ``.env``. Empty is a hard fail here."""
    effective = os.environ if environ is None else environ
    raw = _resolved_raw_value(
        ALLOWED_HOSTS_ENV, effective, _read_dotenv(dotenv_path)
    )
    if raw is None or raw.strip() == "":
        raise PortConfigError(
            f"{ALLOWED_HOSTS_ENV} must name the MagicDNS host for run-agentbox"
        )
    hosts = [part.strip() for part in raw.split(",") if part.strip()]
    if not hosts:
        raise PortConfigError(
            f"{ALLOWED_HOSTS_ENV} was set but named no hostname"
        )
    return hosts


def public_host(hosts: Sequence[str]) -> str:
    """Prefer the Tailscale MagicDNS name when the allowlist has one."""
    for host in hosts:
        if host.endswith(".ts.net"):
            return host
    return hosts[0]


def listener_pids(port: int) -> list[int]:
    """PIDs listening on port, from ``ss`` (no guessed occupants)."""
    result = subprocess.run(
        ["ss", "-ltnpH", f"sport = :{port}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PortConfigError(f"ss failed for port {port}: {result.stderr.strip()}")
    pids: list[int] = []
    for line in result.stdout.splitlines():
        for match in PID_RE.finditer(line):
            pid = int(match.group(1))
            if pid not in pids:
                pids.append(pid)
    return pids


def can_bind(port: int) -> bool:
    """True when a new server listener could take the port.

    Match uvicorn/Vite's address-reuse behavior so a clean restart is not
    blocked by the previous listener's harmless TCP TIME_WAIT sockets.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_bindable(port: int, seconds: float, label: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if can_bind(port):
            return
        time.sleep(0.05)
    raise PortConfigError(f"{label} port {port} did not become bindable")


def _proc_text(pid: int, name: str) -> str:
    path = Path(f"/proc/{pid}/{name}")
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _ppid(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _walk_ancestors(pid: int) -> list[int]:
    seen: list[int] = []
    current: int | None = pid
    for _ in range(8):
        if current is None or current <= 1 or current in seen:
            break
        seen.append(current)
        current = _ppid(current)
    return seen


def _is_our_backend(pid: int) -> bool:
    # uvicorn --reload listens from a multiprocessing spawn whose argv is
    # spawn_main, so walk parents for the apps.webui.server command.
    return any(
        "apps.webui.server" in _proc_text(current, "cmdline")
        for current in _walk_ancestors(pid)
    )


def _is_our_frontend(pid: int) -> bool:
    for current in _walk_ancestors(pid):
        cmdline = _proc_text(current, "cmdline")
        cwd = _proc_cwd(current)
        if "vite" in cmdline and (
            str(PROJECT_ROOT) in cwd or str(PROJECT_ROOT) in cmdline
        ):
            return True
    return False


def _stoppable_ancestor(pid: int) -> bool:
    cmdline = _proc_text(pid, "cmdline")
    cwd = _proc_cwd(pid)
    if "apps.webui.server" in cmdline:
        return True
    if "vite" in cmdline and (
        str(PROJECT_ROOT) in cwd or str(PROJECT_ROOT) in cmdline
    ):
        return True
    return False


def stop_worktree_listeners(ports: WebuiPorts) -> list[str]:
    """Stop this worktree's backend/frontend. Refuse a foreign occupant."""
    notes: list[str] = []
    for service, port, ours in (
        ("backend", ports.backend, _is_our_backend),
        ("frontend", ports.frontend, _is_our_frontend),
    ):
        pids = listener_pids(port)
        if not pids:
            notes.append(f"{service} :{port} idle")
            continue
        strangers = [pid for pid in pids if not ours(pid)]
        if strangers:
            raise PortConfigError(
                f"{service} port {port} is held by pid {strangers}, not this worktree"
            )
        to_stop: list[int] = []
        for pid in pids:
            for current in _walk_ancestors(pid):
                if _stoppable_ancestor(current) and current not in to_stop:
                    to_stop.append(current)
        _stop_pids(to_stop)
        notes.append(f"{service} :{port} stopped pids {to_stop}")
    return notes


def _stop_pids(pids: Sequence[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    alive = set(pids)
    while alive and time.monotonic() < deadline:
        alive = {pid for pid in alive if Path(f"/proc/{pid}").exists()}
        if alive:
            time.sleep(0.05)
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue


def http_status(url: str, *, host: str | None = None, timeout: float = 2.0) -> int:
    """GET url and return the status code. Transport failures are 0."""
    request = urllib.request.Request(url, method="GET")
    if host is not None:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, urllib.error.URLError, ValueError):
        return 0


def https_status(
    url: str, *, resolve_host: str, resolve_ip: str, timeout: float = 3.0
) -> int:
    """Probe Serve with real TLS/SNI even when the box cannot resolve itself."""
    result = subprocess.run(
        [
            "curl",
            "--silent",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--max-time",
            str(timeout),
            "--resolve",
            f"{resolve_host}:{SERVE_HTTPS_PORT}:{resolve_ip}",
            url,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _wait_http(url: str, *, host: str | None, seconds: float, label: str) -> int:
    deadline = time.monotonic() + seconds
    last = 0
    while time.monotonic() < deadline:
        last = http_status(url, host=host)
        if last == 200:
            return last
        time.sleep(0.2)
    raise PortConfigError(f"{label} did not reach 200 (last {last}) at {url}")


def _wait_https(
    url: str, *, resolve_host: str, resolve_ip: str, seconds: float, label: str
) -> int:
    deadline = time.monotonic() + seconds
    last = 0
    while time.monotonic() < deadline:
        last = https_status(
            url, resolve_host=resolve_host, resolve_ip=resolve_ip
        )
        if last == 200:
            return last
        time.sleep(0.2)
    raise PortConfigError(f"{label} did not reach 200 (last {last}) at {url}")


def log_home(*, hostname: str | None = None) -> Path:
    """Agentbox keeps days; other machines use OS temp so a reboot wipes them.

    Repo ``.tmp/`` is gitignored scratch and survives reboot. It is only a
    Cursor symlink, not the cleanup boundary.
    """
    host = socket.gethostname() if hostname is None else hostname
    if host == AGENTBOX_HOSTNAME:
        return LOG_DIR
    return EPHEMERAL_LOG_DIR


def dated_log_path(
    name: str,
    *,
    durable_dir: Path | None = None,
    now: time.struct_time | None = None,
) -> Path:
    """One append-only file per process per local day."""
    day = time.strftime("%Y-%m-%d", now or time.localtime())
    return (durable_dir or log_home()) / f"{name}-{day}.log"


def prepare_log_path(
    name: str,
    *,
    durable_dir: Path | None = None,
    scratch_dir: Path = SCRATCH_DIR,
    now: time.struct_time | None = None,
) -> Path:
    """Dated file plus a worktree ``.tmp/*.log`` symlink for Cursor."""
    durable = dated_log_path(name, durable_dir=durable_dir, now=now)
    durable.parent.mkdir(parents=True, exist_ok=True)
    scratch = scratch_dir / f"{name}.log"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    if scratch.exists() or scratch.is_symlink():
        scratch.unlink()
    scratch.symlink_to(durable)
    return durable


def _start_detached(argv: Sequence[str], *, cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_file.close()
    return proc.pid


def _tailscale_ipv4() -> str:
    result = subprocess.run(
        ["tailscale", "ip", "-4"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PortConfigError(f"tailscale ip -4 failed: {result.stderr.strip()}")
    ip = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not ip:
        raise PortConfigError("tailscale ip -4 returned no address")
    return ip


def ensure_serve(frontend_port: int) -> str:
    """Point tailnet-only HTTPS Serve at this worktree's Vite loopback."""
    target = f"http://127.0.0.1:{frontend_port}"
    result = subprocess.run(
        ["tailscale", "serve", "--bg", f"--https={SERVE_HTTPS_PORT}", target],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PortConfigError(
            f"tailscale serve failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    status = subprocess.run(
        ["tailscale", "serve", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        raise PortConfigError(
            f"tailscale serve status failed: {status.stderr.strip()}"
        )
    try:
        active_ports = json.loads(status.stdout).get("TCP", {})
    except (json.JSONDecodeError, AttributeError) as exc:
        raise PortConfigError("tailscale serve status returned invalid JSON") from exc
    if str(LEGACY_SERVE_HTTP_PORT) in active_ports:
        legacy = subprocess.run(
            [
                "tailscale",
                "serve",
                "--yes",
                f"--http={LEGACY_SERVE_HTTP_PORT}",
                "off",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if legacy.returncode != 0:
            raise PortConfigError(
                "failed to disable insecure legacy Serve endpoint: "
                f"{legacy.stderr.strip() or legacy.stdout.strip()}"
            )
    return target


def is_agentbox(*, hostname: str | None = None) -> bool:
    host = socket.gethostname() if hostname is None else hostname
    return host == AGENTBOX_HOSTNAME or host.startswith(f"{AGENTBOX_HOSTNAME}.")


def allowed_ssh_host(host: str) -> bool:
    """Only the tailnet alias or MagicDNS name. No public IPs, no env override."""
    return host in ALLOWED_SSH_HOSTS


def ssh_agentbox_argv(remote_command: str, *, ssh_host: str = SSH_HOST) -> list[str]:
    """Fixed argv for a BatchMode hop. Caller must pass a constant command."""
    if not allowed_ssh_host(ssh_host):
        raise PortConfigError(
            f"refusing SSH host {ssh_host!r}; allowed {sorted(ALLOWED_SSH_HOSTS)}"
        )
    if not remote_command or any(ch in remote_command for ch in "\n\r"):
        raise PortConfigError("refusing a multiline remote command")
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        ssh_host,
        remote_command,
    ]


def remote_run_command() -> str:
    """Constant remote payload. --local prevents a second hop."""
    return (
        f"test \"$(hostname)\" = {AGENTBOX_HOSTNAME} && "
        f"cd {REMOTE_REPO} && "
        "exec uv run --no-sync python -m apps.webui.run_agentbox --local"
    )


def run_via_ssh(*, ssh_host: str = SSH_HOST) -> int:
    """Start the box from a Mac. Does not bind or serve on the caller."""
    check = subprocess.run(
        ssh_agentbox_argv("hostname", ssh_host=ssh_host),
        check=False,
        capture_output=True,
        text=True,
    )
    remote_name = check.stdout.strip()
    if check.returncode != 0 or remote_name != AGENTBOX_HOSTNAME:
        detail = check.stderr.strip() or remote_name or f"exit {check.returncode}"
        raise PortConfigError(
            f"ssh {ssh_host} did not reach {AGENTBOX_HOSTNAME} ({detail})"
        )
    print(f"[OK] hop ssh {ssh_host} -> {remote_name}")
    hopped = subprocess.run(ssh_agentbox_argv(remote_run_command(), ssh_host=ssh_host))
    if hopped.returncode != 0:
        raise PortConfigError(f"remote run-agentbox exited {hopped.returncode}")
    return 0


def run_agentbox() -> int:
    ports = claim_ports()
    hosts = resolve_allowed_hosts()
    host = public_host(hosts)
    print(f"[OK] reserved {ports.backend}/{ports.frontend} hosts={','.join(hosts)}")
    for note in stop_worktree_listeners(ports):
        print(f"[OK] {note}")
    wait_bindable(ports.backend, STOP_WAIT_SECONDS + 10.0, "backend")
    wait_bindable(ports.frontend, STOP_WAIT_SECONDS + 10.0, "frontend")

    backend_log = prepare_log_path("webui-backend")
    frontend_log = prepare_log_path("webui-frontend")
    client_error_log = prepare_log_path("webui-client-errors")
    print(f"[OK] logs {backend_log}")
    print(f"[OK] logs {frontend_log}")
    print(f"[OK] logs {client_error_log}")

    backend_pid = _start_detached(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "apps.webui.server",
            "--host",
            "127.0.0.1",
            "--reload",
        ],
        cwd=PROJECT_ROOT,
        log_path=backend_log,
    )
    backend_health = f"http://127.0.0.1:{ports.backend}/api/v1/health"
    frontend_root = f"http://127.0.0.1:{ports.frontend}/"
    frontend_health = f"http://127.0.0.1:{ports.frontend}/api/v1/health"
    _wait_http(backend_health, host=None, seconds=BACKEND_WAIT_SECONDS, label="backend")
    print(f"[OK] backend pid {backend_pid} {backend_health}")

    wait_bindable(ports.frontend, STOP_WAIT_SECONDS + 10.0, "frontend")
    frontend_pid = _start_detached(
        ["pnpm", "exec", "vite", "dev", "--host", "127.0.0.1"],
        cwd=FRONTEND_DIR,
        log_path=frontend_log,
    )
    print(f"[OK] started frontend pid {frontend_pid}")
    _wait_http(
        frontend_root, host=host, seconds=FRONTEND_WAIT_SECONDS, label="frontend Host"
    )
    print(f"[OK] frontend {frontend_root} Host={host}")
    _wait_http(
        frontend_health, host=host, seconds=10.0, label="vite /api proxy"
    )
    print(f"[OK] proxy {frontend_health}")

    serve_target = ensure_serve(ports.frontend)
    serve_ip = _tailscale_ipv4()
    serve_url = f"https://{host}/"
    _wait_https(
        serve_url,
        resolve_host=host,
        resolve_ip=serve_ip,
        seconds=15.0,
        label="tailscale HTTPS serve",
    )
    print(f"[OK] serve {serve_target} -> {serve_url} via {serve_ip}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apps.webui.run_agentbox")
    parser.add_argument(
        "--prepare-log",
        metavar="NAME",
        help="create today's append log and print its path (webui-backend|webui-frontend)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="run on this machine (used by the SSH hop; implied on agentbox)",
    )
    args = parser.parse_args(argv)
    try:
        if args.prepare_log:
            print(prepare_log_path(args.prepare_log))
            return 0
        if args.local or is_agentbox():
            return run_agentbox()
        return run_via_ssh()
    except PortConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
