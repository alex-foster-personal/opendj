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
    AGENTBOX_SSH_HOSTS_ENV,
    EPHEMERAL_LOG_DIR,
    LOG_DIR,
    _proc_cwd,
    allowed_ssh_host,
    allowed_ssh_hosts,
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
    serve_url_from_launcher_output,
    ssh_agentbox_argv,
    validate_serve_status,
)
from tests.waits import wait_for_external_state

BROKEN_FUNNEL_STATUS_2026_08_22 = {
    "TCP": {"443": {"HTTPS": True}, "80": {"HTTP": True}},
    "Web": {
        "agentbox.example-tailnet.ts.net:443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8084"}}
        },
        "agentbox.example-tailnet.ts.net:80": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:9080"}}
        },
    },
    "AllowFunnel": {"agentbox.example-tailnet.ts.net:443": True},
}

FIXED_SERVE_STATUS_2026_08_22 = {
    "TCP": {"443": {"HTTPS": True}, "80": {"HTTP": True}},
    "Web": {
        "agentbox.example-tailnet.ts.net:443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:9400"}}
        },
        "agentbox.example-tailnet.ts.net:80": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:9080"}}
        },
    },
}


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


def test_dated_log_path_uses_utc_when_no_time_is_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apps.webui.run_agentbox as runner

    monkeypatch.setattr(runner.time, "gmtime", lambda: time.strptime("2026-08-17", "%Y-%m-%d"))
    monkeypatch.setattr(runner.time, "localtime", lambda: time.strptime("2026-08-18", "%Y-%m-%d"))

    assert dated_log_path("webui-backend", durable_dir=tmp_path) == (
        tmp_path / "webui-backend-2026-08-17.log"
    )


#: A MagicDNS name on a synthetic tailnet. The real label is deployment config
#: (#1540), so the tracked tree uses a placeholder that cannot resolve anywhere.
TAILNET_HOST = "agentbox.example-tailnet.ts.net"


def test_the_ssh_alias_alone_is_a_complete_allowlist() -> None:
    """No MDT_AGENTBOX_SSH_HOSTS is a configured deployment, not a broken one."""
    assert allowed_ssh_host("agentbox", environ={}) is True
    assert allowed_ssh_host("203.0.113.10", environ={}) is False
    assert allowed_ssh_host("evil.example", environ={}) is False


def test_the_tailnet_destination_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A MagicDNS name is allowed only when it was CONFIGURED.

    The shape check this replaces would accept any tailnet whose owner happened
    to name a node "agentbox", so the assertion below is deliberately about
    membership of the configured set and not about the ".ts.net" suffix.
    """
    monkeypatch.setenv(AGENTBOX_SSH_HOSTS_ENV, TAILNET_HOST)
    assert allowed_ssh_host(TAILNET_HOST) is True
    assert allowed_ssh_host("agentbox", environ={AGENTBOX_SSH_HOSTS_ENV: ""}) is True


def test_an_unconfigured_tailnet_destination_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AGENTBOX_SSH_HOSTS_ENV, raising=False)
    assert allowed_ssh_host(TAILNET_HOST) is False
    assert TAILNET_HOST not in allowed_ssh_hosts(environ={})


def test_config_is_parsed_as_an_exact_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AGENTBOX_SSH_HOSTS_ENV, f" {TAILNET_HOST} , agentbox-alt ,, ")
    assert allowed_ssh_hosts() == frozenset({"agentbox", TAILNET_HOST, "agentbox-alt"})


def test_ssh_hop_is_tailnet_only_and_not_a_shell() -> None:
    assert is_agentbox(hostname="agentbox") is True
    assert is_agentbox(hostname="afmac") is False
    assert allowed_ssh_host("agentbox") is True
    assert allowed_ssh_host("203.0.113.10") is False
    assert allowed_ssh_host("evil.example") is False
    with pytest.raises(PortConfigError, match="refusing SSH host"):
        ssh_agentbox_argv("hostname", ssh_host="203.0.113.10")
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


def test_serve_contract_rejects_captured_public_funnel_drift() -> None:
    """The real 22 Aug status must not pass as the private DJ route."""
    with pytest.raises(PortConfigError) as caught:
        validate_serve_status(
            BROKEN_FUNNEL_STATUS_2026_08_22,
            host="agentbox.example-tailnet.ts.net",
            frontend_port=9400,
        )
    message = str(caught.value)
    assert "public Tailscale Funnel is enabled" in message
    assert "targets 'http://127.0.0.1:8084'" in message


def test_serve_contract_accepts_captured_repaired_route() -> None:
    """The real status captured immediately after repair is the contract."""
    validate_serve_status(
        FIXED_SERVE_STATUS_2026_08_22,
        host="agentbox.example-tailnet.ts.net",
        frontend_port=9400,
    )


def test_listener_pids_sees_a_real_loopback_socket() -> None:
    """A real listener on loopback is attributed to the PID that holds it.

    The child announces "bound" on stdout instead of the parent polling a
    3-second timer: spawning a cold interpreter under a loaded full-suite
    run can take longer than that, and the old timer turned it into a
    flake. Waiting on the child's own readiness signal makes the assertion
    a contract check ("once it IS listening, we see it") rather than a
    race, and it fails fast with a real message when the child dies.

    It announces its own PID with that signal rather than the parent reading
    ``Popen.pid``, because the socket belongs to whichever interpreter
    actually called bind: a Windows venv launcher can hand off to the base
    interpreter, and then Popen's PID is the launcher, not the listener. The
    child then holds the port until stdin closes, so releasing it does not
    depend on killing the exact process that owns it either.
    """
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, socket, sys\n"
                f"s = socket.socket(); s.bind(('127.0.0.1', {port})); "
                "s.listen(1)\n"
                "sys.stdout.write(f'bound {os.getpid()}\\n'); sys.stdout.flush()\n"
                "sys.stdin.readline()\n"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        announcement = proc.stdout.readline().split()
        assert announcement[:1] == ["bound"], (
            f"child never bound port {port} (rc={proc.poll()})"
        )
        listener = int(announcement[1])
        seen = listener_pids(port)
        assert listener in seen, (
            f"port {port} is held by {listener} (Popen saw {proc.pid}) "
            f"but listener_pids reported {seen}"
        )
        assert can_bind(port) is False
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.kill()
        proc.wait(timeout=5)
    try:
        wait_for_external_state(
            lambda: can_bind(port),
            what=f"port {port} bindable after its listener pid {listener} exited",
        )
    except AssertionError as timeout:
        holders = listener_pids(port)
        raise AssertionError(
            f"{timeout}; listener_pids({port}) now reports "
            + (f"{holders}" if holders else "no holder, so can_bind refuses a free port")
        ) from timeout


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

pytestmark = pytest.mark.rb_parity


def test_the_served_origin_is_read_back_from_the_launcher() -> None:
    """The public URL comes from what the box REPORTED, never from local config."""
    reported = (
        "[OK] reserved 8585/5173 hosts=agentbox\n"
        "[OK] serve agentbox.example-tailnet.ts.net -> https://agentbox.example-tailnet.ts.net/ via 100.64.0.5\n"
    )
    assert serve_url_from_launcher_output(reported) == (
        "https://agentbox.example-tailnet.ts.net/"
    )


def test_the_last_serve_line_wins() -> None:
    """A replayed log must not hand back an older run's origin."""
    log = (
        "[OK] serve old -> https://old.example-tailnet.ts.net/ via 100.64.0.1\n"
        "[OK] serve new -> https://new.example-tailnet.ts.net/ via 100.64.0.2\n"
    )
    assert serve_url_from_launcher_output(log) == "https://new.example-tailnet.ts.net/"


def test_no_serve_line_is_a_hard_failure() -> None:
    """A restart that never came up must not fall back to a guessed URL."""
    with pytest.raises(PortConfigError, match="no serve line"):
        serve_url_from_launcher_output("[OK] hop ssh agentbox -> agentbox\n")


def test_a_serve_line_without_a_url_is_a_hard_failure() -> None:
    """Near-misses count as absent: the reader is not a substring match."""
    with pytest.raises(PortConfigError, match="no serve line"):
        serve_url_from_launcher_output("[OK] serve agentbox -> nope via 100.64.0.1\n")
