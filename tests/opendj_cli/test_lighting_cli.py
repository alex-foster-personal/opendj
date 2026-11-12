"""``opendj lighting scenes`` and ``look`` HTTP parity (issue #11778, LIGHT-11778).

[if] scenes or look run against a live lighting API [then] stdout matches GET, [else stop].
[if] the engine lock is missing [then] exit 2 names the lock path, [else stop].
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI

from apps.feature_flags import load_flags
from apps.lighting.contract import LIGHTING_FLAG_ID
from apps.lighting.http import bind_lighting_runtime, router
from apps.lighting.runtime import LightingRuntime
from apps.lighting.show_map import list_scene_catalog
from apps.opendj_cli import EXIT_CONFLICT, EXIT_NO_ENGINE, lighting_cli
from apps.opendj_cli.__main__ import main
from apps.opendj_cli.api_cli import _BackendTarget
from tests.waits import start_uvicorn_in_thread


def _lighting_app(tmp_path: Path, *, flag_on: bool, runtime: LightingRuntime | None = None) -> FastAPI:
    flag_dir = tmp_path / "flags"
    flag_dir.mkdir(parents=True, exist_ok=True)
    if flag_on:
        (flag_dir / "feature-flags.json").write_text(json.dumps({LIGHTING_FLAG_ID: True}))
    if runtime is None:
        runtime = LightingRuntime()
    app = FastAPI()
    app.state.feature_flags = load_flags(flag_dir)
    bind_lighting_runtime(app, runtime)
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def lighting_daemon(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    lock_path = tmp_path / "test.engine.lock"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"
    app = _lighting_app(tmp_path, flag_on=True)
    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="lighting daemon",
        sockets=[listener],
    )
    lock_path.write_text(
        json.dumps(
            {
                "version": 1,
                "api_base_url": base_url,
                "pid": 1,
                "role": "test",
            }
        ),
        encoding="utf-8",
    )
    try:
        yield base_url, lock_path
    finally:
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)


@pytest.fixture
def lighting_daemon_flag_off(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    lock_path = tmp_path / "test.engine.lock"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"
    app = _lighting_app(tmp_path, flag_on=False)
    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="lighting daemon",
        sockets=[listener],
    )
    lock_path.write_text(
        json.dumps({"version": 1, "api_base_url": base_url, "pid": 1, "role": "test"}),
        encoding="utf-8",
    )
    try:
        yield base_url, lock_path
    finally:
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)


def _patch_origin(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    monkeypatch.setattr(
        lighting_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: _BackendTarget(base_url=base_url),
    )


@pytest.mark.requirement("LIGHT-11778")
def test_scenes_engine_down(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] engine lock missing [then] scenes exits 2 naming lock, [else stop]."""
    missing = tmp_path / "missing.engine.lock"
    assert main(["--lock", str(missing), "lighting", "scenes"]) == EXIT_NO_ENGINE
    assert str(missing) in capsys.readouterr().err


@pytest.mark.requirement("LIGHT-11778")
def test_look_engine_down(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] engine lock missing [then] look exits 2 naming lock, [else stop]."""
    missing = tmp_path / "missing.engine.lock"
    assert main(["--lock", str(missing), "lighting", "look"]) == EXIT_NO_ENGINE
    assert str(missing) in capsys.readouterr().err


@pytest.mark.requirement("LIGHT-11778")
def test_scenes_json_matches_api(
    lighting_daemon: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] flag on [then] scenes --json ids match GET /lighting/scenes, [else stop]."""
    base_url, lock_path = lighting_daemon
    _patch_origin(monkeypatch, base_url)
    assert main(["--lock", str(lock_path), "--json", "lighting", "scenes"]) == 0
    captured = capsys.readouterr()
    cli_ids = [row["id"] for row in json.loads(captured.out)]
    expected = [row["id"] for row in list_scene_catalog()]
    assert cli_ids == expected


@pytest.mark.requirement("LIGHT-11778")
def test_scenes_refuses_when_flag_off(
    lighting_daemon_flag_off: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] lighting.output off [then] scenes is non-zero and stderr names flag, [else stop]."""
    base_url, lock_path = lighting_daemon_flag_off
    _patch_origin(monkeypatch, base_url)
    assert main(["--lock", str(lock_path), "lighting", "scenes"]) == EXIT_CONFLICT
    assert "lighting.output" in capsys.readouterr().err


@pytest.mark.requirement("LIGHT-11778")
def test_look_json_fields(
    lighting_daemon: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] flag on [then] look --json has scene_id and phrase_label keys, [else stop]."""
    base_url, lock_path = lighting_daemon
    _patch_origin(monkeypatch, base_url)
    assert main(["--lock", str(lock_path), "--json", "lighting", "look"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert "scene_id" in body
    assert "phrase_label" in body
