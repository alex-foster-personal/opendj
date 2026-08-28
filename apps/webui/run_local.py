"""Start this worktree's web UI from one terminal, then open loopback.

macOS: one Terminal.app window per server. Linux: a detached tmux session
with one window per server. Windows has neither host, so this launcher
refuses there instead of half-working. Never binds off loopback; the
browser URL is always ``http://127.0.0.1:<frontend>``.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

LOOPBACK_URL = re.compile(r"^http://127\.0\.0\.1:(\d+)$")

from apps.webui.port_config import PROJECT_ROOT, PortConfigError, claim_ports
from apps.webui.run_agentbox import FRONTEND_WAIT_SECONDS, _wait_http, http_status


def localhost_url(port: int) -> str:
    if not isinstance(port, int) or isinstance(port, bool) or port < 1024 or port > 65_535:
        raise PortConfigError(f"refusing to open a non-dev port: {port!r}")
    return f"http://127.0.0.1:{port}"


def just_bin() -> str:
    found = shutil.which("just")
    if found is None:
        raise PortConfigError("just is not on PATH")
    return found


def service_command(service: str, *, repo: Path, just: str) -> str:
    """One POSIX shell line that runs a service from the repo.

    Both hosts this launcher has -- Terminal.app and tmux -- take a POSIX
    shell line, and ``cd ... && exec ...`` is not something cmd.exe or
    PowerShell can run. Windows gets an explicit refusal naming the two
    commands to run by hand, rather than a string that cannot work and a
    failure further downstream.
    """
    if os.name == "nt":
        raise PortConfigError(
            "run-local needs a POSIX shell host (Terminal.app or tmux) and "
            "Windows has neither; run 'just webui-backend' and "
            "'just webui-frontend' in two shells instead"
        )
    if service not in {"backend", "frontend"}:
        raise PortConfigError(f"unknown service {service!r}")
    return (
        f"cd {shlex.quote(str(repo))} && exec {shlex.quote(just)} webui-{service}"
    )


def tmux_session_name(frontend_port: int) -> str:
    return f"mdt-webui-{frontend_port}"


def terminal_do_script_argv(command: str) -> list[str]:
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    return [
        "osascript",
        "-e",
        f'tell application "Terminal" to do script "{escaped}"',
    ]


def tmux_start_argv(session: str, backend_cmd: str, frontend_cmd: str) -> list[list[str]]:
    return [
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            "backend",
            backend_cmd,
        ],
        [
            "tmux",
            "new-window",
            "-t",
            session,
            "-n",
            "frontend",
            frontend_cmd,
        ],
    ]


def _tmux_has_session(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _spawn_macos(backend_cmd: str, frontend_cmd: str) -> None:
    for command in (backend_cmd, frontend_cmd):
        launched = subprocess.run(terminal_do_script_argv(command), check=False)
        if launched.returncode != 0:
            raise PortConfigError("Terminal.app refused do script")


def _spawn_tmux(session: str, backend_cmd: str, frontend_cmd: str) -> None:
    if shutil.which("tmux") is None:
        raise PortConfigError("tmux is not on PATH; install it or run on macOS")
    if _tmux_has_session(session):
        print(f"[OK] tmux session {session} already running")
        return
    for argv in tmux_start_argv(session, backend_cmd, frontend_cmd):
        started = subprocess.run(argv, check=False, capture_output=True, text=True)
        if started.returncode != 0:
            raise PortConfigError(
                f"tmux failed: {started.stderr.strip() or started.stdout.strip()}"
            )
    print(f"[OK] tmux new-session {session} (attach: tmux attach -t {session})")


def open_localhost(url: str) -> None:
    if LOOPBACK_URL.fullmatch(url) is None:
        raise PortConfigError(f"refusing to open a non-loopback URL: {url}")
    if sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
        return
    print(f"open {url}")


def run_local(*, repo: Path = PROJECT_ROOT) -> int:
    ports = claim_ports()
    url = localhost_url(ports.frontend)
    print(f"[OK] reserved {ports.backend}/{ports.frontend}")
    print(f"[OK] frontend {url}")
    if http_status(url) == 200:
        print("[OK] frontend already answering")
        open_localhost(url)
        return 0

    just = just_bin()
    backend_cmd = service_command("backend", repo=repo, just=just)
    frontend_cmd = service_command("frontend", repo=repo, just=just)
    if sys.platform == "darwin":
        _spawn_macos(backend_cmd, frontend_cmd)
    else:
        _spawn_tmux(tmux_session_name(ports.frontend), backend_cmd, frontend_cmd)

    _wait_http(url, host=None, seconds=FRONTEND_WAIT_SECONDS, label="frontend")
    print(f"[OK] frontend ready {url}")
    open_localhost(url)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apps.webui.run_local")
    parser.parse_args(argv)
    try:
        return run_local()
    except PortConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
