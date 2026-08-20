"""Regression tests for the worktree-local web UI port contract."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apps.webui.port_config import BACKEND_ENV, FRONTEND_ENV, PortConfigError, WebuiPorts
from apps.webui.server import __main__ as server_cli


@pytest.fixture(autouse=True)
def _own_the_port_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand both port env keys to monkeypatch before any test calls main().

    ``server_cli.main`` deliberately writes ``os.environ[BACKEND_ENV]`` (and
    ``FRONTEND_ENV`` on the claim path) so the app can read its own port back.
    Without this fixture those writes outlive the test and leak into every
    later test in the same process: ``test_writeback_cli`` used to pass only
    because this module happened to run first and left 8697 behind. Declaring
    the keys here makes monkeypatch restore the pre-test state on teardown.
    """
    monkeypatch.delenv(BACKEND_ENV, raising=False)
    monkeypatch.delenv(FRONTEND_ENV, raising=False)


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
            "application": "apps.webui.server.app:app",
            "host": "127.0.0.1",
            "port": 8697,
            "reload": False,
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
