"""NOWP-11489: opendj now-playing CLI (issue #10336).

[if] the CLI JSON or watch output disagrees with GET /api/v1/now-playing [then] fail, [else stop].
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_NO_ENGINE, api_cli, now_playing_cli
from apps.webui.server.routes import now_playing, state
from tests.waits import start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("NOWP-11489")


def _mini_app() -> FastAPI:
    app = FastAPI()
    app.include_router(state.router, prefix="/api/v1")
    app.include_router(now_playing.router, prefix="/api/v1")
    return app


def _heard_mirror(stable_id: str = "sid-1", gain: float = 1.0) -> dict[str, Any]:
    return {
        "client_open": True,
        "decks": {
            "1": {
                "stable_id": stable_id,
                "title": "Track One",
                "artist": "Artist One",
                "key": "Am",
                "bpm": 124.0,
                "playing": True,
                "position": {"ms": 0},
            }
        },
        "mixer": {"crossfader": 0.5, "channels": {"1": {"fader": 1.0, "assign": "THRU", "on_air_gain": gain}}},
        "room_output": {"speakers_on": True, "room_path": "room_only"},
    }


def _put_mirror(base_url: str, stable_id: str, *, steps: int = 4) -> None:
    with httpx.Client(timeout=15.0) as client:
        for _ in range(steps):
            client.put(f"{base_url}/api/v1/state/ui-mirror", json=_heard_mirror(stable_id))


@pytest.fixture
def loopback_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Real uvicorn socket; only ``resolve_backend_base_url`` is patched."""
    mono = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0])

    def next_mono() -> float:
        return next(mono, 12.0)

    monkeypatch.setattr(state, "_received_monotonic_s", next_mono)
    monkeypatch.setattr(now_playing, "_now_monotonic", lambda: 12.0)

    app = _mini_app()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, log_level="warning"), what="the engine", sockets=[listener]
    )
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(
        now_playing_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=base_url),
    )
    lock = tmp_path / ".engine.lock"
    lock.write_text(
        json.dumps({"pid": 4242, "role": "opendj-engine", "host": "127.0.0.1", "port": port}),
        encoding="utf-8",
    )
    try:
        yield base_url, lock
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_json_stdout_matches_route_text_verbatim(
    loopback_engine: tuple[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """One-shot --json uses real GET; stdout must equal response.text, not a re-serialize."""
    base_url, lock = loopback_engine
    _put_mirror(base_url, "sid-1")
    with httpx.Client(timeout=15.0) as client:
        route_text = client.get(f"{base_url}/api/v1/now-playing").text
    code = now_playing_cli.run(["--json"], lock=lock)
    out = capsys.readouterr().out
    assert code == EXIT_CONFIRMED
    assert out.endswith("\n")
    assert out[:-1] == route_text


def test_watch_json_prints_three_loadable_lines(
    loopback_engine: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--watch --json: real fetches; one JSON line per generation change."""
    base_url, lock = loopback_engine
    phase = {"n": 0}

    def sleep_and_advance(_interval: float) -> None:
        if phase["n"] == 0:
            _put_mirror(base_url, "sid-1")
            phase["n"] = 1
        elif phase["n"] == 1:
            _put_mirror(base_url, "sid-2")
            phase["n"] = 2
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(now_playing_cli, "_sleep", sleep_and_advance)
    code = now_playing_cli.run(["--watch", "--json"], lock=lock)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert code == EXIT_CONFIRMED
    assert len(lines) == 3
    for line in lines:
        json.loads(line)


def test_format_line_variants() -> None:
    full = now_playing_cli.format_line(
        {
            "stale": False,
            "track": {
                "stable_id": "s",
                "artist": "A",
                "title": "T",
                "key_camelot": "8A",
                "key_musical": "Am",
                "bpm": 124.0,
                "deck": 2,
                "since": "2026-11-10T00:00:00.000Z",
            },
        }
    )
    assert "A - T" in full and "deck 2" in full
    no_artist = now_playing_cli.format_line(
        {"stale": False, "track": {"stable_id": "s", "artist": None, "title": "T", "deck": 1}}
    )
    assert "None" not in no_artist
    stale = now_playing_cli.format_line({"stale": True, "track": None, "previous": [], "generation": 0})
    assert stale.endswith("(stale)")


def test_engine_not_running(tmp_path: Path) -> None:
    lock = tmp_path / "missing.lock"
    lock.write_text("{}", encoding="utf-8")
    assert now_playing_cli.run([], lock=lock) == EXIT_NO_ENGINE
