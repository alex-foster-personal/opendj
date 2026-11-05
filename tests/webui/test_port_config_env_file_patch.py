"""Patching ``port_config.WEBUI_ENV_FILE`` reaches every root ``.env`` resolver.

Tests isolate themselves from the worktree's ``.env`` (often a symlink to the
primary checkout's, naming that checkout's daemon ports) by monkeypatching
``port_config.WEBUI_ENV_FILE``. The resolvers used to default
``dotenv_path: Path = WEBUI_ENV_FILE``, which binds at definition time, so the
patch never reached them: a CLI test then dialed the primary's port instead of
its own engine's lock file and failed with "Connection refused".

Each case writes a ``.env`` the real worktree cannot contain and points the
module global at it, so it goes red against the definition-time default
whether or not this checkout has a root ``.env`` of its own.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.opendj_cli.api_cli import resolve_backend_base_url
from apps.shared.allowed_hosts import ALLOWED_HOSTS_ENV
from apps.webui import port_config
from apps.webui.port_config import (
    BACKEND_ENV,
    FRONTEND_ENV,
    PortConfigError,
    resolve_backend_port,
    resolve_frontend_port,
    resolve_ports,
)
from apps.webui.run_agentbox import resolve_allowed_hosts
from apps.webui.server.request_guard import resolve_allowed_hosts_from_environ

# [8800, 9400) sits between the lane-0 backend and frontend pools and every
# later lane starts above it, so no claimed worktree .env can name these ports.
PATCHED_BACKEND = 9101
PATCHED_FRONTEND = 9102
EXPLICIT_BACKEND = 9111
PATCHED_HOST = "patched-env-file.example-tailnet.ts.net"
EXPLICIT_HOST = "explicit-dotenv-path.example-tailnet.ts.net"


def _write_env(path: Path, backend: int, frontend: int, host: str) -> Path:
    path.write_text(
        f"{BACKEND_ENV}={backend}\n{FRONTEND_ENV}={frontend}\n{ALLOWED_HOSTS_ENV}={host}\n",
        encoding="utf-8",
    )
    return path


RESOLVERS: dict[str, tuple[Callable[[], object], object]] = {
    "resolve_ports.backend": (lambda: resolve_ports(environ={}).backend, PATCHED_BACKEND),
    "resolve_ports.frontend": (lambda: resolve_ports(environ={}).frontend, PATCHED_FRONTEND),
    "resolve_backend_port": (lambda: resolve_backend_port(None, environ={}), PATCHED_BACKEND),
    "resolve_frontend_port": (lambda: resolve_frontend_port(environ={}), PATCHED_FRONTEND),
    "request_guard.resolve_allowed_hosts_from_environ": (
        lambda: resolve_allowed_hosts_from_environ({}),
        [PATCHED_HOST],
    ),
    "run_agentbox.resolve_allowed_hosts": (
        lambda: resolve_allowed_hosts(environ={}),
        [PATCHED_HOST],
    ),
    "api_cli.resolve_backend_base_url": (
        lambda: resolve_backend_base_url({}).base_url,
        f"http://127.0.0.1:{PATCHED_BACKEND}",
    ),
}


@pytest.mark.parametrize("name", sorted(RESOLVERS))
def test_patched_env_file_is_the_one_the_resolver_reads(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve, expected = RESOLVERS[name]
    patched = _write_env(tmp_path / ".env", PATCHED_BACKEND, PATCHED_FRONTEND, PATCHED_HOST)
    monkeypatch.setattr(port_config, "WEBUI_ENV_FILE", patched)
    assert resolve() == expected


def test_an_explicit_dotenv_path_still_beats_the_module_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overshoot control: the call-time default must not override a passed path."""
    patched = _write_env(tmp_path / "patched.env", PATCHED_BACKEND, PATCHED_FRONTEND, PATCHED_HOST)
    explicit = _write_env(tmp_path / "explicit.env", EXPLICIT_BACKEND, PATCHED_FRONTEND, EXPLICIT_HOST)
    monkeypatch.setattr(port_config, "WEBUI_ENV_FILE", patched)
    assert resolve_ports(environ={}, dotenv_path=explicit).backend == EXPLICIT_BACKEND
    assert resolve_backend_port(None, environ={}, dotenv_path=explicit) == EXPLICIT_BACKEND
    assert resolve_allowed_hosts_from_environ({}, dotenv_path=explicit) == [EXPLICIT_HOST]
    assert resolve_allowed_hosts(environ={}, dotenv_path=explicit) == [EXPLICIT_HOST]


def test_process_environment_still_beats_the_patched_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Precedence control: shell > root ``.env`` survives the default change."""
    patched = _write_env(tmp_path / ".env", PATCHED_BACKEND, PATCHED_FRONTEND, PATCHED_HOST)
    monkeypatch.setattr(port_config, "WEBUI_ENV_FILE", patched)
    environ = {BACKEND_ENV: str(EXPLICIT_BACKEND), FRONTEND_ENV: str(PATCHED_FRONTEND)}
    assert resolve_ports(environ=environ).backend == EXPLICIT_BACKEND


def test_an_absent_patched_env_file_sends_the_cli_to_the_engine_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The isolation the CLI tests rely on: no ports anywhere -> the lock file wins."""
    monkeypatch.setattr(port_config, "WEBUI_ENV_FILE", tmp_path / "no.env")
    with pytest.raises(PortConfigError):
        resolve_ports(environ={})
    lock = tmp_path / ".engine.lock"
    lock.write_text(json.dumps({"host": "127.0.0.1", "port": 9121}), encoding="utf-8")
    target = resolve_backend_base_url({}, lock_path=lock)
    assert target.origin is not None
    assert target.base_url == "http://127.0.0.1:9121"
