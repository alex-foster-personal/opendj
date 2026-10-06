"""Regression tests for the worktree-local web UI port contract."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apps.shared.uvicorn_shutdown import GRACEFUL_SHUTDOWN_S
from apps.webui.port_config import BACKEND_ENV, FRONTEND_ENV, PortConfigError, WebuiPorts
from apps.webui.server import __main__ as server_cli


@pytest.fixture(autouse=True)
def _restore_port_env() -> Iterator[None]:
    """Put the two port env keys back exactly as this module found them.

    ``server_cli.main`` deliberately writes ``os.environ[BACKEND_ENV]`` (and
    ``FRONTEND_ENV`` on the claim path) so the app can read its own port back.
    That is real production behavior and is not stubbed here -- what is fixed
    is that the write used to outlive the test and leak into the rest of the
    process. ``tests/webui/test_writeback_cli.py`` passed only because this
    module happened to run first and left 8697 behind, which serial ordering
    hid and parallel execution exposed.
    """
    before = {name: os.environ.get(name) for name in (BACKEND_ENV, FRONTEND_ENV)}
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            elif os.environ.get(name) != value:
                os.environ[name] = value


def _capture_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _run(application: str, **kwargs: Any) -> None:
        calls.append({"application": application, **kwargs})

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=_run))
    return calls


def _stub_worktree_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    ports = WebuiPorts(backend=8697, frontend=9411)
    monkeypatch.setattr(server_cli, "claim_ports", lambda: ports)
    monkeypatch.setattr(server_cli, "check_reservation", lambda service: ports)


def test_server_uses_worktree_backend_port_when_cli_port_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the CLI omits --port then the root worktree contract owns the port."""
    _stub_worktree_claim(monkeypatch)
    calls = _capture_uvicorn(monkeypatch)

    assert server_cli.main([]) == 0
    assert calls == [
        {
            "application": "apps.webui.server.app:create_process_app",
            "host": "127.0.0.1",
            "port": 8697,
            "reload": False,
            "factory": True,
            "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_S,
        }
    ]


def test_cli_port_overrides_worktree_backend_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If --port is explicit then CLI precedence beats the worktree config."""
    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", "8697")
    calls = _capture_uvicorn(monkeypatch)

    assert server_cli.main(["--prod", "--port", "8765"]) == 0
    assert calls[0]["port"] == 8765


@pytest.mark.parametrize("value", ["", "zero", "0", "65536"])
def test_invalid_worktree_backend_port_fails_before_server_start(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """If the worktree port is invalid then startup is broken and must fail fast."""
    def _raise_invalid_config() -> WebuiPorts:
        raise PortConfigError(f"MUSIC_DJ_BACKEND_PORT is invalid: {value!r}")

    monkeypatch.setattr(server_cli, "claim_ports", _raise_invalid_config)
    calls = _capture_uvicorn(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        server_cli.main([])

    assert exc_info.value.code == 2
    assert calls == []


def test_missing_worktree_backend_port_requires_explicit_cli_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If neither env nor CLI defines a port then hidden fallback behavior is broken."""
    monkeypatch.delenv("MUSIC_DJ_BACKEND_PORT", raising=False)
    monkeypatch.setattr(server_cli, "WEBUI_ENV_FILE", tmp_path / "missing.env")
    calls = _capture_uvicorn(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        server_cli.main(["--prod"])

    assert exc_info.value.code == 2
    assert calls == []


def _assert_bounded_graceful_shutdown(calls: list[dict[str, Any]]) -> None:
    """The one assertion both the real and the mutation-control test share."""
    assert len(calls) == 1
    assert calls[0].get("timeout_graceful_shutdown") == GRACEFUL_SHUTDOWN_S


@pytest.mark.requirement("INSTALL-35")
def test_server_bounds_graceful_shutdown_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The webui daemon's uvicorn.run gets the same bounded shutdown apps.engine_core uses.

    [if] apps.webui.server boots [then] uvicorn.run gets timeout_graceful_shutdown, [else stop].
    """
    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", "8697")
    calls = _capture_uvicorn(monkeypatch)

    assert server_cli.main(["--prod"]) == 0
    _assert_bounded_graceful_shutdown(calls)


@pytest.mark.requirement("INSTALL-35")
def test_missing_graceful_shutdown_timeout_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation control for the test above: a pre-fix-shaped call must fail it.

    [if] uvicorn.run is called with no timeout_graceful_shutdown [then] the shared check fails, [else stop].

    Without this, the assertion in test_server_bounds_graceful_shutdown_window
    could pass no matter what uvicorn.run was actually given.
    """
    pre_fix_call: dict[str, Any] = {
        "application": "apps.webui.server.app:create_process_app",
        "host": "127.0.0.1",
        "port": 8697,
        "reload": False,
        "factory": True,
    }
    with pytest.raises(AssertionError):
        _assert_bounded_graceful_shutdown([pre_fix_call])


pytestmark = pytest.mark.rb_parity
