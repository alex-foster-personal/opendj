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
import shutil
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
    resolve_ports,
)

ALLOWED_HOSTS_ENV = "MUSIC_DJ_ALLOWED_HOSTS"
SERVE_HTTPS_PORT = 443
SERVE_HTTP_PORT = 80
LEGACY_SERVE_HTTP_PORT = 8080
HTTP_REDIRECT_PORT = 9080
BACKEND_WAIT_SECONDS = 30.0
FRONTEND_WAIT_SECONDS = 45.0
STOP_WAIT_SECONDS = 5.0
PID_RE = re.compile(r"pid=(\d+)")
# netstat's foreign-address column for a socket with no peer: a listener.
WINDOWS_IDLE_PEERS = frozenset({"0.0.0.0:0", "[::]:0", "*:*"})
LOG_DIR = Path.home() / ".local/share/music-dj-tools/webui"
SCRATCH_DIR = PROJECT_ROOT / ".tmp"
EPHEMERAL_LOG_DIR = Path(tempfile.gettempdir()) / "music-dj-tools"
FRONTEND_DIR = PROJECT_ROOT / "apps" / "webui" / "frontend"
AGENTBOX_HOSTNAME = "agentbox"
#: Env naming this deployment's agentbox SSH destinations, comma separated, in
#: addition to the bare ssh alias. The MagicDNS name embeds a TAILNET label,
#: which is deployment-specific, so it is CONFIGURED rather than spelled in the
#: tracked tree (#1540). Reading the PROCESS environment is deliberate: the
#: worktree root .env holds the two port variables and is rewritten by the port
#: tooling, so it is not this contract's home.
#:
#: A SHAPE check ("ends in .ts.net") would not do: any tailnet whose owner named
#: a node "agentbox" satisfies it, and crate_sync hands an approved destination
#: straight to rsync without re-verifying the remote hostname. Only exact
#: membership of this allowlist stands between a typo and a library copied to
#: somebody else's machine.
AGENTBOX_SSH_HOSTS_ENV = "MDT_AGENTBOX_SSH_HOSTS"
SSH_HOST = "agentbox"
REMOTE_REPO = "/root/music-dj-tools"
SSH_CONNECT_TIMEOUT = "8"
CLIENT_LOG_NAMES = frozenset({"webui-client-errors", "webui-visitors"})


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


#: The one line that reports the origin the box ACTUALLY served, and the pattern
#: that reads it back. A caller must not re-derive the public host from local
#: config: the MagicDNS name is deployment config (#1540), and a URL guessed here
#: is how a restart that never came up behind HTTPS looks successful.
SERVE_LINE_RE = re.compile(r"^\[OK\] serve \S+ -> (https://\S+) via \S+\s*$", re.MULTILINE)


def serve_url_from_launcher_output(text: str) -> str:
    """The HTTPS origin the launcher reported, or a hard failure.

    Deliberately no fallback. The LAST match wins, so a caller replaying an
    accumulated log gets the run it just made rather than an older one.
    """
    matches = SERVE_LINE_RE.findall(text)
    if not matches:
        raise PortConfigError(
            "run-agentbox printed no serve line, so the box never came up behind "
            "HTTPS serve; there is no public origin to open"
        )
    return matches[-1]


def _windows_listener_pids(port: int) -> list[int]:
    """PIDs listening on ``port``, read from ``netstat -ano``.

    Windows ships neither ``ss`` nor ``lsof``; ``netstat`` is in System32 on
    every install. Rows read ``TCP <local> <foreign> LISTENING <pid>``, and a
    listener is identified by its empty foreign address rather than by the
    state word, which netstat translates on a localised Windows. A row for a
    CONNECTION to this port carries a real peer there, so the same test also
    keeps established sockets out of the answer.
    """
    result = subprocess.run(
        ["netstat", "-ano"], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise PortConfigError(f"netstat failed for port {port}: {detail}")
    pids: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 5 or not fields[0].upper().startswith("TCP"):
            continue
        _protocol, local, foreign, _state, raw_pid = fields
        if not local.endswith(f":{port}") or foreign not in WINDOWS_IDLE_PEERS:
            continue
        if not raw_pid.isdigit():
            continue
        pid = int(raw_pid)
        if pid not in pids:
            pids.append(pid)
    return pids


def listener_pids(port: int) -> list[int]:
    """PIDs listening on port, from ``ss``, ``lsof`` or ``netstat``.

    The agentbox itself is Linux, where ``ss`` is the right tool. The same
    launcher is driven from macOS during development, which has no ``ss``
    at all, and from Windows, which has neither -- an undecorated
    ``FileNotFoundError`` from the subprocess is a worse diagnostic than
    "the port is busy", so dispatch on whichever probe the host actually has
    and fail loudly when it has none.
    """
    if os.name == "nt":
        return _windows_listener_pids(port)
    if shutil.which("ss"):
        argv = ["ss", "-ltnpH", f"sport = :{port}"]
    elif shutil.which("lsof"):
        # -b is not optional: without it lsof stats every mounted filesystem,
        # which on a Mac carrying iCloud or network mounts blocks for tens of
        # seconds, trips lsof's own 15s alarm and makes it abandon the scan
        # and print NOTHING -- an occupied port silently reported as free.
        # -b skips those blocking kernel calls; a network query never needed
        # them. Measured 30.1s/17.6s/0.05s unflagged vs a flat 0.03s with -b.
        argv = ["lsof", "-b", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]
    else:
        raise PortConfigError(
            f"cannot read listeners on port {port}: neither 'ss' nor 'lsof' "
            "is on PATH"
        )
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    # lsof exits 1 with no output when nothing holds the port; that is an
    # empty answer, not a failure. ss reports the same state with exit 0.
    # An empty answer that came WITH a warning is not an answer at all --
    # that is the under-report case, so refuse it rather than call the port
    # free.
    stderr = result.stderr.strip()
    if not result.stdout.strip() and stderr:
        raise PortConfigError(f"{argv[0]} could not read port {port}: {stderr}")
    if result.returncode != 0 and (argv[0] != "lsof" or stderr):
        raise PortConfigError(f"{argv[0]} failed for port {port}: {stderr}")
    pids: list[int] = []
    for line in result.stdout.splitlines():
        # ss tags the holder as ``pid=1234``; lsof -t prints the bare PID.
        found = PID_RE.findall(line) or ([line.strip()] if line.strip().isdigit() else [])
        for raw in found:
            pid = int(raw)
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


def _is_our_redirect(pid: int) -> bool:
    return any(
        "apps.agentbox.http_redirect" in _proc_text(current, "cmdline")
        for current in _walk_ancestors(pid)
    )


def _stoppable_ancestor(pid: int) -> bool:
    cmdline = _proc_text(pid, "cmdline")
    cwd = _proc_cwd(pid)
    if "apps.webui.server" in cmdline:
        return True
    if "apps.agentbox.http_redirect" in cmdline:
        return True
    if "vite" in cmdline and (
        str(PROJECT_ROOT) in cwd or str(PROJECT_ROOT) in cmdline
    ):
        return True
    return False


def stop_worktree_listeners(ports: WebuiPorts) -> list[str]:
    """Stop this worktree's web listeners. Refuse a foreign occupant."""
    notes: list[str] = []
    for service, port, ours in (
        ("HTTP redirect", HTTP_REDIRECT_PORT, _is_our_redirect),
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def redirect_response(
    url: str, *, host: str | None = None, timeout: float = 2.0
) -> tuple[int, str | None]:
    """Return a real HTTP response without following its redirect."""
    request = urllib.request.Request(url, method="GET")
    if host is not None:
        request.add_header("Host", host)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.headers.get("Location")
    except (OSError, urllib.error.URLError, ValueError):
        return 0, None


def _wait_redirect(
    url: str,
    *,
    host: str | None,
    expected_location: str,
    seconds: float,
    label: str,
) -> None:
    deadline = time.monotonic() + seconds
    last: tuple[int, str | None] = (0, None)
    while time.monotonic() < deadline:
        last = redirect_response(url, host=host)
        if last == (307, expected_location):
            return
        time.sleep(0.2)
    raise PortConfigError(
        f"{label} did not redirect to {expected_location} (last {last}) at {url}"
    )


def log_home(*, hostname: str | None = None) -> Path:
    """Agentbox keeps days; other machines use OS temp so a reboot wipes them.

    Repo ``.tmp/`` is gitignored scratch and survives reboot. It is only a
    Cursor symlink, not the cleanup boundary.
    """
    host = socket.gethostname() if hostname is None else hostname
    if host == AGENTBOX_HOSTNAME:
        return LOG_DIR
    return EPHEMERAL_LOG_DIR


def log_dir_for(name: str, *, hostname: str | None = None) -> Path:
    """Browser-originated diagnostics persist; process logs are host-scoped."""
    if name in CLIENT_LOG_NAMES:
        return LOG_DIR
    return log_home(hostname=hostname)


def dated_log_path(
    name: str,
    *,
    durable_dir: Path | None = None,
    now: time.struct_time | None = None,
) -> Path:
    """One append-only file per process per UTC day."""
    day = time.strftime("%Y-%m-%d", now or time.gmtime())
    return (durable_dir or log_dir_for(name)) / f"{name}-{day}.log"


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


def _serve_status() -> Mapping[str, object]:
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
        payload = json.loads(status.stdout)
    except json.JSONDecodeError as exc:
        raise PortConfigError("tailscale serve status returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PortConfigError("tailscale serve status returned a non-object")
    return payload


def validate_serve_status(
    status: Mapping[str, object],
    *,
    host: str,
    frontend_port: int,
    redirect_port: int = HTTP_REDIRECT_PORT,
) -> None:
    """Fail closed when 80/443 drift from the private agentbox contract."""
    https_key = f"{host}:{SERVE_HTTPS_PORT}"
    http_key = f"{host}:{SERVE_HTTP_PORT}"
    problems: list[str] = []
    allow_funnel = status.get("AllowFunnel", {})
    if isinstance(allow_funnel, dict) and allow_funnel.get(https_key) is True:
        problems.append(
            f"public Tailscale Funnel is enabled on {https_key}; "
            "expected tailnet-only Serve"
        )

    web = status.get("Web", {})
    if not isinstance(web, dict):
        raise PortConfigError("tailscale serve status has no Web routes")

    def proxy_for(key: str) -> object:
        route = web.get(key, {})
        handlers = route.get("Handlers", {}) if isinstance(route, dict) else {}
        root = handlers.get("/", {}) if isinstance(handlers, dict) else {}
        return root.get("Proxy") if isinstance(root, dict) else None

    expected_https = f"http://127.0.0.1:{frontend_port}"
    actual_https = proxy_for(https_key)
    if actual_https != expected_https:
        problems.append(
            f"tailscale HTTPS route {https_key} targets {actual_https!r}; "
            f"expected {expected_https!r}"
        )
    expected_http = f"http://127.0.0.1:{redirect_port}"
    actual_http = proxy_for(http_key)
    if actual_http != expected_http:
        problems.append(
            f"tailscale HTTP route {http_key} targets {actual_http!r}; "
            f"expected {expected_http!r}"
        )
    if problems:
        raise PortConfigError("; ".join(problems))


def ensure_serve(
    frontend_port: int,
    *,
    host: str,
    redirect_port: int = HTTP_REDIRECT_PORT,
) -> str:
    """Serve secure Vite plus a tailnet-only HTTP-to-HTTPS redirect."""
    funnel = subprocess.run(
        [
            "tailscale",
            "funnel",
            "--yes",
            f"--https={SERVE_HTTPS_PORT}",
            "off",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if funnel.returncode != 0:
        raise PortConfigError(
            "failed to disable public Funnel on the web port: "
            f"{funnel.stderr.strip() or funnel.stdout.strip()}"
        )
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
    redirect_target = f"http://127.0.0.1:{redirect_port}"
    redirect = subprocess.run(
        [
            "tailscale",
            "serve",
            "--bg",
            f"--http={SERVE_HTTP_PORT}",
            redirect_target,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if redirect.returncode != 0:
        raise PortConfigError(
            "tailscale HTTP redirect serve failed: "
            f"{redirect.stderr.strip() or redirect.stdout.strip()}"
        )
    status = _serve_status()
    active_ports = status.get("TCP", {})
    if not isinstance(active_ports, dict):
        raise PortConfigError("tailscale serve status has no TCP routes")
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
        status = _serve_status()
    validate_serve_status(
        status,
        host=host,
        frontend_port=frontend_port,
        redirect_port=redirect_port,
    )
    return target


def is_agentbox(*, hostname: str | None = None) -> bool:
    host = socket.gethostname() if hostname is None else hostname
    return host == AGENTBOX_HOSTNAME or host.startswith(f"{AGENTBOX_HOSTNAME}.")


def allowed_ssh_hosts(*, environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """The ssh alias plus this deployment's configured MagicDNS destinations.

    Empty is not an error here, unlike ``resolve_allowed_hosts``: the bare alias
    is a complete allowlist for a deployment that reaches agentbox by name, so a
    machine with no extra configuration is configured correctly rather than
    silently unconfigured.
    """
    effective = os.environ if environ is None else environ
    raw = effective.get(AGENTBOX_SSH_HOSTS_ENV, "")
    extra = {part.strip() for part in raw.split(",") if part.strip()}
    return frozenset({AGENTBOX_HOSTNAME}) | extra


def allowed_ssh_host(host: str, *, environ: Mapping[str, str] | None = None) -> bool:
    """Exact membership of the configured allowlist. No public IPs, no shape match."""
    return host in allowed_ssh_hosts(environ=environ)


def allowed_ssh_hosts_description(*, environ: Mapping[str, str] | None = None) -> str:
    """The allowlist, for an error message a human has to act on."""
    return f"{sorted(allowed_ssh_hosts(environ=environ))} ({AGENTBOX_SSH_HOSTS_ENV})"


def ssh_agentbox_argv(remote_command: str, *, ssh_host: str = SSH_HOST) -> list[str]:
    """Fixed argv for a BatchMode hop. Caller must pass a constant command."""
    if not allowed_ssh_host(ssh_host):
        raise PortConfigError(
            f"refusing SSH host {ssh_host!r}; allowed {allowed_ssh_hosts_description()}"
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


def _validated_stem_track(stable_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", stable_id):
        raise PortConfigError(f"invalid stem stable id {stable_id!r}")
    return stable_id


def remote_check_command(stem_track: str | None = None) -> str:
    """Constant remote browser-check payload. --local prevents a second hop."""
    command = (
        f"test \"$(hostname)\" = {AGENTBOX_HOSTNAME} && "
        f"cd {REMOTE_REPO} && "
        "exec uv run --no-sync python -m apps.webui.run_agentbox --local --check"
    )
    if stem_track is not None:
        command += f" --stem-track {_validated_stem_track(stem_track)}"
    return command


def run_via_ssh(
    *, ssh_host: str = SSH_HOST, remote_command: str | None = None
) -> int:
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
    command = remote_run_command() if remote_command is None else remote_command
    hopped = subprocess.run(
        ssh_agentbox_argv(command, ssh_host=ssh_host), check=False
    )
    if hopped.returncode != 0:
        raise PortConfigError(f"remote run-agentbox exited {hopped.returncode}")
    return 0


def check_agentbox(stem_track: str | None = None) -> int:
    """Prove HTTP aliases, TLS, browser JS, AudioWorklet, IPC, API and telemetry."""
    hosts = resolve_allowed_hosts()
    host = public_host(hosts)
    serve_ip = _tailscale_ipv4()
    validate_serve_status(
        _serve_status(),
        host=host,
        frontend_port=resolve_ports().frontend,
    )
    command = [
            "pnpm",
            "exec",
            "node",
            "scripts/agentbox-check.mjs",
            "--host",
            host,
            "--ip",
            serve_ip,
        ]
    if stem_track is not None:
        command.extend(["--stem-track", _validated_stem_track(stem_track)])
    result = subprocess.run(
        command,
        cwd=FRONTEND_DIR,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PortConfigError(f"agentbox browser check failed: {detail}")
    print(f"[OK] browser {result.stdout.strip()}")
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
    wait_bindable(HTTP_REDIRECT_PORT, STOP_WAIT_SECONDS + 10.0, "HTTP redirect")

    backend_log = prepare_log_path("webui-backend")
    frontend_log = prepare_log_path("webui-frontend")
    client_error_log = prepare_log_path("webui-client-errors")
    visitor_log = prepare_log_path("webui-visitors")
    redirect_log = prepare_log_path("agentbox-http-redirect")
    print(f"[OK] logs {backend_log}")
    print(f"[OK] logs {frontend_log}")
    print(f"[OK] logs {client_error_log}")
    print(f"[OK] logs {visitor_log}")
    print(f"[OK] logs {redirect_log}")

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

    secure_origin = f"https://{host}"
    redirect_pid = _start_detached(
        [
            sys.executable,
            "-m",
            "apps.agentbox.http_redirect",
            "--host",
            "127.0.0.1",
            "--port",
            str(HTTP_REDIRECT_PORT),
            "--target-origin",
            secure_origin,
        ],
        cwd=PROJECT_ROOT,
        log_path=redirect_log,
    )
    expected_redirect = f"{secure_origin}/performance"
    _wait_redirect(
        f"http://127.0.0.1:{HTTP_REDIRECT_PORT}/performance",
        host="agentbox",
        expected_location=expected_redirect,
        seconds=10.0,
        label="loopback HTTP redirect",
    )
    print(f"[OK] HTTP redirect pid {redirect_pid} -> {secure_origin}")

    serve_target = ensure_serve(ports.frontend, host=host)
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
    _wait_redirect(
        f"http://{serve_ip}/performance",
        host="agentbox",
        expected_location=expected_redirect,
        seconds=10.0,
        label="tailscale short-host HTTP redirect",
    )
    print(f"[OK] http://agentbox/performance -> {expected_redirect}")
    check_agentbox()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apps.webui.run_agentbox")
    parser.add_argument(
        "--prepare-log",
        metavar="NAME",
        help="create today's append log and print its path",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="run on this machine (used by the SSH hop; implied on agentbox)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="prove the existing tailnet Serve path in a real browser",
    )
    parser.add_argument(
        "--stem-track",
        help="with --check, load this real track and prove its stem graph",
    )
    args = parser.parse_args(argv)
    try:
        if args.prepare_log:
            print(prepare_log_path(args.prepare_log))
            return 0
        if args.check:
            if args.local or is_agentbox():
                return check_agentbox(args.stem_track)
            return run_via_ssh(
                remote_command=remote_check_command(args.stem_track)
            )
        if args.stem_track:
            raise PortConfigError("--stem-track requires --check")
        if args.local or is_agentbox():
            return run_agentbox()
        return run_via_ssh()
    except PortConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
