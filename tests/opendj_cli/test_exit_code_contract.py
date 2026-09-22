"""Cross-verb exit-code contract for ``opendj`` (issue #3776, LIBM-11).

[if] the engine is down [then] status, open, and api all exit 2, [else stop].
"""

from __future__ import annotations

import inspect
import json
import socket
from pathlib import Path

import pytest

from apps import opendj_cli as pkg
from apps.opendj_cli import EXIT_FAILED, EXIT_NO_ENGINE, api_cli
from apps.opendj_cli.__main__ import main
from apps.webui import port_config


def _no_worktree_ports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MUSIC_DJ_BACKEND_PORT", raising=False)
    monkeypatch.delenv("MUSIC_DJ_FRONTEND_PORT", raising=False)
    monkeypatch.setattr(port_config, "WEBUI_ENV_FILE", tmp_path / "no.env")


@pytest.mark.requirement("LIBM-11")
def test_status_exits_no_engine_when_lock_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] status with a missing lock [then] exit 2 names the lock file, [else stop]."""
    missing = tmp_path / "absent" / ".engine.lock"
    assert main(["--lock", str(missing), "status"]) == EXIT_NO_ENGINE
    captured = capsys.readouterr()
    assert str(missing) in captured.err


@pytest.mark.requirement("LIBM-11")
def test_open_exits_no_engine_when_lock_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] open with a missing lock [then] exit 2 names the lock file, [else stop]."""
    missing = tmp_path / "absent" / ".engine.lock"
    assert main(["--lock", str(missing), "open", "performance"]) == EXIT_NO_ENGINE
    captured = capsys.readouterr()
    assert str(missing) in captured.err


@pytest.mark.requirement("LIBM-11")
def test_api_exits_no_engine_when_lock_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] api with a missing lock [then] exit 2 names the lock file, [else stop]."""
    missing = tmp_path / "absent" / ".engine.lock"
    _no_worktree_ports(monkeypatch, tmp_path)
    assert (
        main(["--lock", str(missing), "api", "GET", "/api/v1/health"])
        == EXIT_NO_ENGINE
    )
    captured = capsys.readouterr()
    assert str(missing) in captured.err


@pytest.mark.requirement("LIBM-11")
def test_api_exits_no_engine_when_lock_port_dead(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] api lock names a dead port [then] exit 2 not 1, [else stop]."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = int(probe.getsockname()[1])
    lock = tmp_path / ".engine.lock"
    lock.write_text(
        json.dumps({"pid": 1, "host": "127.0.0.1", "port": dead_port}),
        encoding="utf-8",
    )
    assert (
        main(["--lock", str(lock), "api", "GET", "/api/v1/health"])
        == EXIT_NO_ENGINE
    )
    captured = capsys.readouterr()
    assert str(lock) in captured.err


@pytest.mark.requirement("LIBM-11")
def test_api_bad_method_never_exits_no_engine(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] api gets an unknown HTTP method [then] exit 1 never 2, [else stop]."""
    code = main(["api", "FLY", "/api/v1/health"])
    assert code == EXIT_FAILED
    assert code != EXIT_NO_ENGINE


def test_api_cli_imports_canonical_exit_constants() -> None:
    assert "EXIT_USAGE" not in api_cli.__dict__
    assert api_cli.exit_for_status(412) == pkg.EXIT_PRECONDITION
    assert pkg.EXIT_NO_ENGINE == 2
    assert pkg.EXIT_FAILED == 1
    source = inspect.getsource(api_cli)
    assert "EXIT_USAGE = 2" not in source
