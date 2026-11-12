"""Density CLI acceptance through a real daemon and persisted UI preferences.

[if] density is read or changed [then] the real preference route is used, [else stop].
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
import uvicorn
from fastapi import Request
from starlette.responses import Response

from apps.opendj_cli import EXIT_FAILED, EXIT_OK, api_cli
from apps.opendj_cli.__main__ import main
from apps.shared.state import db as state_db
from apps.webui import port_config
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("SEARCH-13466")
PREFS_ROUTE = "/api/v1/ui-prefs"


@dataclass
class DensityDaemon:
    base_url: str
    lock_path: Path
    prefs_path: Path
    requests: list[tuple[str, str]]


@pytest.fixture
def density_daemon(tmp_path: Path) -> Iterator[DensityDaemon]:
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_db.open_rw(state_path).close()
    app = create_app(
        backend=SqliteBackend(state_path),
        state_db_path=str(state_path),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path / "client-errors",
        client_event_log_dir=tmp_path / "client-events",
    )
    app.state.data_dir = data_dir
    requests: list[tuple[str, str]] = []

    @app.middleware("http")
    async def observe_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        requests.append((request.method, request.url.path))
        return await call_next(request)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]
        lock_path = tmp_path / ".engine.lock"
        lock_path.write_text(json.dumps({"pid": 1, "host": "127.0.0.1", "port": port}), encoding="utf-8")
        server, thread = start_uvicorn_in_thread(
            uvicorn.Config(app, log_level="warning"),
            what="the density daemon",
            sockets=[listener],
        )
        try:
            yield DensityDaemon(
                f"http://127.0.0.1:{port}",
                lock_path,
                data_dir / "state" / "ui-prefs.json",
                requests,
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "density daemon did not stop"


@pytest.mark.parametrize("density", ["compact", "cosy"])
@pytest.mark.parametrize("json_output", [False, True])
def test_density_persists_and_reads_back(
    density_daemon: DensityDaemon,
    density: str,
    json_output: bool,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = density_daemon
    # An explicit lock must win over a different worktree target.
    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", "1")
    response = httpx.put(
        daemon.base_url + PREFS_ROUTE,
        json={"library_density": "cosy" if density == "compact" else "compact", "hide_broken_links": True},
        timeout=5,
    )
    assert response.status_code == 200
    daemon.requests.clear()
    prefix = ["--lock", str(daemon.lock_path)]
    if json_output:
        prefix.insert(0, "--json")
    assert main([*prefix, "density", density]) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["library_density"] == density
    assert daemon.requests == [("PUT", PREFS_ROUTE)]
    stored = json.loads(daemon.prefs_path.read_text(encoding="utf-8"))
    assert stored["library_density"] == density
    assert stored["hide_broken_links"] is True
    before = daemon.prefs_path.read_bytes()
    assert main([*prefix, "density"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["library_density"] == density
    assert daemon.prefs_path.read_bytes() == before
    response = httpx.get(daemon.base_url + PREFS_ROUTE, timeout=5)
    assert response.status_code == 200
    assert response.json()["library_density"] == density


def test_density_read_defaults_without_writing(
    density_daemon: DensityDaemon,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = density_daemon
    daemon.requests.clear()
    assert main(["--lock", str(daemon.lock_path), "density"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["library_density"] == "compact"
    assert daemon.requests == [("GET", PREFS_ROUTE)]
    assert not daemon.prefs_path.exists()


@pytest.mark.parametrize("rest", [["wide"], ["cosy", "extra"], ["--bogus"], ["Compact"]])
@pytest.mark.parametrize("seeded", [False, True])
def test_density_invalid_arguments_never_reach_http(
    density_daemon: DensityDaemon,
    rest: list[str],
    seeded: bool,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = density_daemon
    if seeded:
        response = httpx.put(
            daemon.base_url + PREFS_ROUTE,
            json={"library_density": "cosy"},
            timeout=5,
        )
        assert response.status_code == 200
    before = daemon.prefs_path.read_bytes() if seeded else None
    daemon.requests.clear()

    def refuse_resolution(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("invalid density reached backend discovery")

    monkeypatch.setattr(api_cli, "resolve_backend_base_url", refuse_resolution)
    assert main(["--lock", str(daemon.lock_path), "density", *rest]) == EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "compact|cosy" in captured.err
    assert daemon.requests == []
    assert (daemon.prefs_path.read_bytes() if daemon.prefs_path.exists() else None) == before


def test_density_route_rejects_invalid_value(density_daemon: DensityDaemon) -> None:
    daemon = density_daemon
    response = httpx.put(
        daemon.base_url + PREFS_ROUTE,
        json={"library_density": "cosy"},
        timeout=5,
    )
    assert response.status_code == 200
    before = daemon.prefs_path.read_bytes()
    response = httpx.put(
        daemon.base_url + PREFS_ROUTE,
        json={"library_density": "wide"},
        timeout=5,
    )
    assert response.status_code == 422
    assert daemon.prefs_path.read_bytes() == before


def test_density_token_in_api_arguments_does_not_hijack_command(
    density_daemon: DensityDaemon,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = density_daemon
    daemon.requests.clear()
    assert (
        main(
            [
                "--lock",
                str(daemon.lock_path),
                "api",
                "GET",
                PREFS_ROUTE,
                "density",
            ]
        )
        == EXIT_FAILED
    )
    assert "unexpected argument 'density'" in capsys.readouterr().err
    assert daemon.requests == []


def test_density_token_as_lock_value_is_not_the_command(
    density_daemon: DensityDaemon,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    daemon = density_daemon
    (tmp_path / "density").write_bytes(daemon.lock_path.read_bytes())
    monkeypatch.chdir(tmp_path)
    daemon.requests.clear()
    assert main(["--lock", "density", "api", "GET", PREFS_ROUTE]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["library_density"] == "compact"
    assert daemon.requests == [("GET", PREFS_ROUTE)]


def test_density_runner_uses_injected_worktree_environment(
    density_daemon: DensityDaemon,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    daemon = density_daemon
    port = int(daemon.base_url.rsplit(":", 1)[1])
    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", str(port))
    monkeypatch.setenv("MUSIC_DJ_FRONTEND_PORT", str(port + 1))
    monkeypatch.setattr(port_config, "WEBUI_ENV_FILE", tmp_path / "worktree.env")
    assert api_cli.run_density(["cosy"], as_json=True) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["library_density"] == "cosy"
    assert json.loads(daemon.prefs_path.read_text(encoding="utf-8"))["library_density"] == "cosy"


def test_density_list_verbs_never_requests_preferences(
    density_daemon: DensityDaemon,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = density_daemon
    daemon.requests.clear()
    assert main(["--list-verbs", "density", "cosy"]) == EXIT_OK
    assert "opendj density" in capsys.readouterr().out
    assert daemon.requests == []


def test_density_cli_process_round_trip(density_daemon: DensityDaemon) -> None:
    daemon = density_daemon
    command = [sys.executable, "-m", "apps.opendj_cli", "--lock", str(daemon.lock_path), "density"]
    for density in ("cosy", "compact"):
        write = subprocess.run([*command, density], capture_output=True, text=True, timeout=30, check=False)
        assert write.returncode == EXIT_OK, write.stderr
        assert json.loads(write.stdout)["library_density"] == density
        read = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        assert read.returncode == EXIT_OK, read.stderr
        assert json.loads(read.stdout)["library_density"] == density
    before = daemon.prefs_path.read_bytes()
    invalid = subprocess.run([*command, "wide"], capture_output=True, text=True, timeout=30, check=False)
    assert invalid.returncode == EXIT_FAILED
    assert daemon.prefs_path.read_bytes() == before
