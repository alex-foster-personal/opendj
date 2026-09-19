"""``opendj api METHOD PATH``: raw HTTP escape hatch (LIBM-11).

Runs against a real in-process app (no mocks).

[if] GET /health on a live backend [then] stdout is JSON and exit 0
[if] GET a missing route [then] body is on stderr and exit 1
[if] the daemon is not reachable [then] stderr names the port and exit 1
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.responses import HTMLResponse

from apps.opendj_cli import api_cli
from apps.opendj_cli.__main__ import main
from apps.webui import port_config
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from tests.opendj_cli.conftest import Engine
from tests.waits import start_uvicorn_in_thread


@pytest.fixture
def library_daemon(tmp_path: Path) -> Iterator[tuple[str, int]]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path / "client-errors",
        client_event_log_dir=tmp_path / "client-events",
    )
    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="the library daemon",
        sockets=[listener],
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, port
    finally:
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)


def _patch_backend(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=base_url),
    )


def _no_worktree_ports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MUSIC_DJ_BACKEND_PORT", raising=False)
    monkeypatch.delenv("MUSIC_DJ_FRONTEND_PORT", raising=False)
    monkeypatch.setattr(port_config, "WEBUI_ENV_FILE", tmp_path / "no.env")


def test_api_get_prints_json_and_exits_0(
    library_daemon: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_url, _port = library_daemon
    _patch_backend(monkeypatch, base_url)

    assert main(["api", "GET", "/api/v1/health"]) == api_cli.EXIT_OK

    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["status"] == "ok"
    assert captured.err == ""


@pytest.fixture
def html_api_daemon(tmp_path: Path) -> Iterator[tuple[str, int]]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path / "client-errors",
        client_event_log_dir=tmp_path / "client-events",
    )

    @app.get("/api/v1/test-html-3035")
    def _html() -> HTMLResponse:
        return HTMLResponse("<html></html>")

    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="the html api daemon",
        sockets=[listener],
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, port
    finally:
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)


@pytest.mark.requirement("AGENT-11")
def test_api_rejects_traversal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] the api path climbs out of /api/v1 [then] exit failed with no stdout, [else stop]."""
    assert (
        main(["api", "GET", "/api/v1/../../../etc/passwd"])
        == api_cli.EXIT_FAILED
    )
    assert capsys.readouterr().out == ""


@pytest.mark.requirement("AGENT-11")
def test_api_rejects_root_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] the api path is bare / [then] exit failed with no stdout, [else stop]."""
    assert main(["api", "GET", "/"]) == api_cli.EXIT_FAILED
    assert capsys.readouterr().out == ""


@pytest.mark.requirement("AGENT-11")
def test_api_invalid_path_emits_json_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] --json and the path is invalid [then] stdout carries code invalid_path, [else stop]."""
    assert main(["--json", "api", "GET", "/"]) == api_cli.EXIT_FAILED
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "invalid_path"


@pytest.mark.requirement("AGENT-11")
def test_api_rejects_html_success_body(
    html_api_daemon: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] a 200 body is HTML not JSON [then] exit failed and stderr says not JSON, [else stop]."""
    base_url, _port = html_api_daemon
    _patch_backend(monkeypatch, base_url)

    assert main(["api", "GET", "/api/v1/test-html-3035"]) == api_cli.EXIT_FAILED

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not JSON" in captured.err


@pytest.mark.requirement("AGENT-11")
def test_api_health_still_accepts_json(
    html_api_daemon: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] the health route answers JSON [then] exit ok and print the body, [else stop]."""
    base_url, _port = html_api_daemon
    _patch_backend(monkeypatch, base_url)

    assert main(["api", "GET", "/api/v1/health"]) == api_cli.EXIT_OK

    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["status"] == "ok"
    assert captured.err == ""


def test_api_get_404_prints_body_to_stderr_and_exits_1(
    library_daemon: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_url, _port = library_daemon
    _patch_backend(monkeypatch, base_url)

    assert main(["api", "GET", "/api/v1/no-such-route-for-libm-11"]) == api_cli.EXIT_FAILED

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""


def test_api_unreachable_names_the_port(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = int(probe.getsockname()[1])

    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(
            base_url=f"http://127.0.0.1:{dead_port}"
        ),
    )

    assert main(["api", "GET", "/api/v1/health"]) == api_cli.EXIT_FAILED

    captured = capsys.readouterr()
    assert str(dead_port) in captured.err
    assert "not reachable" in captured.err


def test_exit_for_status_maps_412_and_409() -> None:
    assert api_cli.exit_for_status(200) == api_cli.EXIT_OK
    assert api_cli.exit_for_status(404) == api_cli.EXIT_FAILED
    assert api_cli.exit_for_status(412) == api_cli.EXIT_PRECONDITION
    assert api_cli.exit_for_status(409) == api_cli.EXIT_CONFLICT


def test_parse_request_accepts_repeatable_headers() -> None:
    parsed = api_cli._parse_request([
        "PUT", "/api/v1/playlists/pl-1/tracks",
        "-H", 'If-Match: "etag-1"',
        "--json", '{"stable_ids":[]}',
    ])
    assert parsed.extra_headers == (("If-Match", '"etag-1"'),)


def test_api_patch_with_json_body(
    library_daemon: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_url, _port = library_daemon
    _patch_backend(monkeypatch, base_url)

    code = main(
        [
            "api",
            "PATCH",
            "/api/v1/settings/ai-search",
            "--json",
            '{"query":"test","limit":1}',
        ]
    )
    assert code in {api_cli.EXIT_OK, api_cli.EXIT_FAILED}
    captured = capsys.readouterr()
    assert captured.out or captured.err


def test_resolve_backend_base_url_uses_lock_when_ports_missing(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_worktree_ports(monkeypatch, tmp_path)

    target = api_cli.resolve_backend_base_url(lock_path=engine.lock_path)

    assert target.base_url == engine.base_url
    assert target.origin is not None
    assert target.origin.lock_path == engine.lock_path


def test_api_get_via_lock_file(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_worktree_ports(monkeypatch, tmp_path)

    assert (
        main(["--lock", str(engine.lock_path), "api", "GET", "/api/v1/health"])
        == api_cli.EXIT_OK
    )

    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["status"] == "ok"
    assert captured.err == ""


def test_api_without_ports_or_lock_names_lock_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "absent" / ".engine.lock"
    _no_worktree_ports(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENDJ_LIVE_LOCK_PATH", str(missing))

    assert main(["api", "GET", "/api/v1/health"]) == api_cli.EXIT_USAGE

    captured = capsys.readouterr()
    assert str(missing) in captured.err
    assert "MUSIC_DJ_BACKEND_PORT" not in captured.err


def test_api_dead_lock_names_lock_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
        == api_cli.EXIT_FAILED
    )

    captured = capsys.readouterr()
    assert str(lock) in captured.err
    assert str(dead_port) in captured.err
    assert "engine lock file" in captured.err


@pytest.mark.requirement("AGENT-05")
def test_api_ui_prefs_topbar_round_trip(
    library_daemon: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] opendj api PUTs beat_sync_max [then] GET returns the same value, [else stop]."""
    base_url, _port = library_daemon
    _patch_backend(monkeypatch, base_url)

    put_code = main(
        [
            "api",
            "PUT",
            "/api/v1/ui-prefs",
            "--json",
            '{"beat_sync_max": false, "auto_play_enabled": false}',
        ]
    )
    assert put_code == api_cli.EXIT_OK
    capsys.readouterr()

    assert main(["api", "GET", "/api/v1/ui-prefs"]) == api_cli.EXIT_OK
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["beat_sync_max"] is False
    assert body["auto_play_enabled"] is False


@pytest.mark.requirement("AGENT-05")
def test_api_ui_prefs_library_browser_round_trip(
    library_daemon: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] opendj api PUTs hide_broken_links and wheel_sensitivity [then] GET matches, [else stop]."""
    base_url, _port = library_daemon
    _patch_backend(monkeypatch, base_url)

    put_code = main(
        [
            "api",
            "PUT",
            "/api/v1/ui-prefs",
            "--json",
            '{"hide_broken_links": true, "wheel_sensitivity": {"mouse": 2.0, "trackpad": 0.2}}',
        ]
    )
    assert put_code == api_cli.EXIT_OK
    capsys.readouterr()

    assert main(["api", "GET", "/api/v1/ui-prefs"]) == api_cli.EXIT_OK
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["hide_broken_links"] is True
    assert body["wheel_sensitivity"]["mouse"] == 2.0
    assert body["wheel_sensitivity"]["trackpad"] == 0.2


def test_api_prefers_worktree_port_over_lock(
    library_daemon: tuple[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _base_url, live_port = library_daemon
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = int(probe.getsockname()[1])
    lock = tmp_path / ".engine.lock"
    lock.write_text(
        json.dumps({"pid": 1, "host": "127.0.0.1", "port": dead_port}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", str(live_port))
    monkeypatch.setenv("MUSIC_DJ_FRONTEND_PORT", str(live_port + 1))

    assert main(["--lock", str(lock), "api", "GET", "/api/v1/health"]) == api_cli.EXIT_OK

    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["status"] == "ok"
    assert captured.err == ""
