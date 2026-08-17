"""Real-socket coverage for the agentbox runner helpers."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from apps.webui.port_config import PortConfigError
from apps.webui.run_agentbox import (
    EPHEMERAL_LOG_DIR,
    LOG_DIR,
    _proc_cwd,
    allowed_ssh_host,
    can_bind,
    dated_log_path,
    http_status,
    https_status,
    is_agentbox,
    listener_pids,
    log_dir_for,
    log_home,
    prepare_log_path,
    public_host,
    redirect_response,
    remote_check_command,
    remote_run_command,
    resolve_allowed_hosts,
    ssh_agentbox_argv,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_allowed_hosts_come_from_root_env_and_refuse_blank(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("MUSIC_DJ_ALLOWED_HOSTS=\n", encoding="utf-8")
    with pytest.raises(PortConfigError, match="must name the MagicDNS host"):
        resolve_allowed_hosts(environ={}, dotenv_path=env_path)

    env_path.write_text(
        "MUSIC_DJ_ALLOWED_HOSTS=agentbox,agentbox.example-tailnet.ts.net\n",
        encoding="utf-8",
    )
    assert resolve_allowed_hosts(environ={}, dotenv_path=env_path) == [
        "agentbox",
        "agentbox.example-tailnet.ts.net",
    ]
    assert (
        resolve_allowed_hosts(
            environ={"MUSIC_DJ_ALLOWED_HOSTS": "shell-host"},
            dotenv_path=env_path,
        )
        == ["shell-host"]
    )


def test_log_home_is_durable_only_on_agentbox() -> None:
    assert log_home(hostname="agentbox") == LOG_DIR
    assert log_home(hostname="afmac") == EPHEMERAL_LOG_DIR
    assert log_home(hostname="bifrost2") == EPHEMERAL_LOG_DIR
    assert log_dir_for("webui-visitors", hostname="afmac") == LOG_DIR
    assert log_dir_for("webui-client-errors", hostname="afmac") == LOG_DIR
    assert log_dir_for("webui-backend", hostname="afmac") == EPHEMERAL_LOG_DIR


def test_dated_logs_append_off_the_worktree(tmp_path: Path) -> None:
    durable_dir = tmp_path / "durable"
    scratch_dir = tmp_path / "scratch"
    now = time.strptime("2026-08-17", "%Y-%m-%d")
    assert dated_log_path("webui-backend", durable_dir=durable_dir, now=now) == (
        durable_dir / "webui-backend-2026-08-17.log"
    )
    path = prepare_log_path(
        "webui-backend",
        durable_dir=durable_dir,
        scratch_dir=scratch_dir,
        now=now,
    )
    assert path == durable_dir / "webui-backend-2026-08-17.log"
    scratch = scratch_dir / "webui-backend.log"
    assert scratch.is_symlink()
    assert scratch.resolve() == path
    path.write_text("first\n", encoding="utf-8")
    prepare_log_path(
        "webui-backend",
        durable_dir=durable_dir,
        scratch_dir=scratch_dir,
        now=now,
    )
    assert path.read_text(encoding="utf-8") == "first\n"


def test_ssh_hop_is_tailnet_only_and_not_a_shell() -> None:
    assert is_agentbox(hostname="agentbox") is True
    assert is_agentbox(hostname="afmac") is False
    assert allowed_ssh_host("agentbox") is True
    assert allowed_ssh_host("agentbox.example-tailnet.ts.net") is True
    assert allowed_ssh_host("198.51.100.7") is False
    assert allowed_ssh_host("evil.example") is False
    with pytest.raises(PortConfigError, match="refusing SSH host"):
        ssh_agentbox_argv("hostname", ssh_host="198.51.100.7")
    argv = ssh_agentbox_argv("hostname")
    assert argv[:6] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "agentbox",
    ]
    assert argv[-1] == "hostname"
    remote = remote_run_command()
    assert "--local" in remote
    assert "\n" not in remote
    assert remote.startswith('test "$(hostname)" = agentbox')
    check_remote = remote_check_command()
    assert "--local --check" in check_remote
    assert "\n" not in check_remote
    stem_remote = remote_check_command("track-001")
    assert stem_remote.endswith("--stem-track track-001")
    with pytest.raises(PortConfigError, match="invalid stem stable id"):
        remote_check_command("track; rm -rf nope")


def test_public_host_prefers_magicdns() -> None:
    assert public_host(["agentbox", "agentbox.example-tailnet.ts.net"]) == (
        "agentbox.example-tailnet.ts.net"
    )
    assert public_host(["agentbox"]) == "agentbox"


def test_listener_pids_sees_a_real_loopback_socket() -> None:
    """A real listener on loopback is attributed to the PID that holds it.

    The child announces "bound" on stdout instead of the parent polling a
    3-second timer: spawning a cold interpreter under a loaded full-suite
    run can take longer than that, and the old timer turned it into a
    flake. Waiting on the child's own readiness signal makes the assertion
    a contract check ("once it IS listening, we see it") rather than a
    race, and it fails fast with a real message when the child dies.
    """
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket, sys, time\n"
                f"s = socket.socket(); s.bind(('127.0.0.1', {port})); "
                "s.listen(1)\n"
                "sys.stdout.write('bound\\n'); sys.stdout.flush()\n"
                "time.sleep(30)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "bound", (
            f"child never bound port {port} (rc={proc.poll()})"
        )
        assert proc.pid in listener_pids(port)
        assert can_bind(port) is False
    finally:
        proc.kill()
        proc.wait(timeout=2)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not can_bind(port):
        time.sleep(0.05)
    assert can_bind(port) is True


def test_proc_cwd_reads_the_real_process_symlink() -> None:
    assert Path(_proc_cwd(os.getpid())).resolve() == Path.cwd().resolve()


def test_http_status_reads_a_real_server() -> None:
    port = _free_port()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            host = self.headers.get("Host", "")
            body = b"ok" if "agentbox" in host else b"no-host"
            self.send_response(200 if body == b"ok" else 400)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/"
        assert http_status(url, host="agentbox.example-tailnet.ts.net") == 200
        assert http_status(url, host="127.0.0.1") == 400
        assert http_status("http://127.0.0.1:9/", host=None) == 0
    finally:
        server.shutdown()
        server.server_close()


def test_redirect_response_does_not_follow_a_real_redirect() -> None:
    port = _free_port()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(307)
            self.send_header(
                "Location",
                "https://agentbox.example-tailnet.ts.net/performance",
            )
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert redirect_response(
            f"http://127.0.0.1:{port}/performance", host="agentbox"
        ) == (307, "https://agentbox.example-tailnet.ts.net/performance")
    finally:
        server.shutdown()
        server.server_close()


def test_https_status_returns_zero_for_a_real_closed_socket() -> None:
    port = _free_port()
    assert https_status(
        f"https://agentbox.example-tailnet.ts.net:{port}/",
        resolve_host="agentbox.example-tailnet.ts.net",
        resolve_ip="127.0.0.1",
        timeout=0.2,
    ) == 0
