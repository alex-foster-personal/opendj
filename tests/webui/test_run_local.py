"""Builders for ``just run``: loopback URL, Terminal.app, tmux."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.webui.port_config import PortConfigError
from apps.webui.run_local import (
    localhost_url,
    open_localhost,
    service_command,
    terminal_do_script_argv,
    tmux_session_name,
    tmux_start_argv,
)


def test_localhost_url_is_loopback_only() -> None:
    assert localhost_url(9400) == "http://127.0.0.1:9400"
    with pytest.raises(PortConfigError, match="non-dev port"):
        localhost_url(80)
    with pytest.raises(PortConfigError, match="non-dev port"):
        localhost_url(True)  # type: ignore[arg-type]


def test_open_localhost_refuses_non_loopback() -> None:
    with pytest.raises(PortConfigError, match="non-loopback"):
        open_localhost("https://agentbox.example-tailnet.ts.net")
    with pytest.raises(PortConfigError, match="non-loopback"):
        open_localhost("http://127.0.0.1:9400/../")


def test_service_command_stays_in_the_repo(tmp_path: Path) -> None:
    cmd = service_command("backend", repo=tmp_path, just="/opt/homebrew/bin/just")
    assert cmd.startswith(f"cd {tmp_path}")
    assert cmd.endswith("exec /opt/homebrew/bin/just webui-backend")
    with pytest.raises(PortConfigError, match="unknown service"):
        service_command("all", repo=tmp_path, just="just")


def test_macos_and_tmux_argv_are_literal() -> None:
    backend = "cd /repo && exec just webui-backend"
    frontend = "cd /repo && exec just webui-frontend"
    osa = terminal_do_script_argv(backend)
    assert osa[0] == "osascript"
    assert 'do script "cd /repo && exec just webui-backend"' in osa[-1]
    quoted = terminal_do_script_argv('echo "hi"')
    assert r'do script "echo \"hi\""' in quoted[-1]
    session = tmux_session_name(9400)
    assert session == "mdt-webui-9400"
    new_session, new_window = tmux_start_argv(session, backend, frontend)
    assert new_session[:6] == ["tmux", "new-session", "-d", "-s", session, "-n"]
    assert new_session[-1] == backend
    assert new_window[:5] == ["tmux", "new-window", "-t", session, "-n"]
    assert new_window[-1] == frontend
