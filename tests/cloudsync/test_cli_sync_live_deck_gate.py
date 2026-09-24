"""CLOUDSYNC-14: the standalone CLI must see a live engine's playing deck.

Found by review on PR #3831 (Codex, Tue 22 Sep 2026): ``python -m
apps.sync_hub sync`` always called ``sync(..., ui_mirror=None)``, so
``refuse_sync_round`` could never detect ``any_deck_playing`` from the CLI --
only the in-process HTTP route (which reads ``Request.app.state.ui_mirror``)
honored it. An operator running the CLI by hand while a real engine has a
deck playing would defer only for Gig posture, never for a playing deck,
contradicting the shipped CLOUDSYNC-14 requirement text verbatim.

[if] a live engine's mirror shows a playing deck [then] the CLI sync command defers with deck_playing, [else stop].
[if] no engine lock file exists at --data-dir [then] the CLI mirror probe returns None, [else stop].
[if] a live engine has no open performance page (409) [then] the CLI mirror probe returns None, [else stop].
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from apps.sync_hub import maintenance
from tests.waits import start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("CLOUDSYNC-14")

_BOOT_ID = "test-boot-cloudsync14"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_lock(data_dir: Path, *, port: int) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".engine.lock").write_text(
        json.dumps(
            {
                "pid": 1,
                "role": "opendj-engine",
                "boot_id": _BOOT_ID,
                "host": "127.0.0.1",
                "port": port,
            }
        ),
        encoding="utf-8",
    )


def _fake_engine_app(mirror_status: int, mirror_body: dict) -> FastAPI:
    """A minimal stand-in for the real engine's health + ui-mirror routes."""
    app = FastAPI()

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"boot_id": _BOOT_ID}

    @app.get("/api/v1/state/ui-mirror")
    def ui_mirror() -> JSONResponse:
        return JSONResponse(status_code=mirror_status, content=mirror_body)

    return app


@pytest.fixture
def live_engine(request: pytest.FixtureRequest) -> Iterator[str]:
    """A real HTTP server on the loopback answering as the engine would."""
    mirror_status, mirror_body = request.param
    port = _free_port()
    config = uvicorn.Config(
        _fake_engine_app(mirror_status, mirror_body),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server, thread = start_uvicorn_in_thread(config, what="the fake engine")
    try:
        yield str(port)
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def test_no_lock_file_means_nothing_can_be_playing(tmp_path: Path) -> None:
    """[if] no engine lock file [then] the CLI mirror probe returns None."""
    assert maintenance._cli_live_ui_mirror(tmp_path) is None


@pytest.mark.parametrize("live_engine", [(200, {"decks": {"1": {"playing": True}}})], indirect=True)
def test_live_engine_mirror_is_read_when_verified(tmp_path: Path, live_engine: str) -> None:
    """[if] a verified live engine answers 200 [then] its mirror body is returned."""
    _write_lock(tmp_path, port=int(live_engine))
    mirror = maintenance._cli_live_ui_mirror(tmp_path)
    assert mirror == {"decks": {"1": {"playing": True}}}


@pytest.mark.parametrize("live_engine", [(409, {"client_open": False})], indirect=True)
def test_no_open_page_means_nothing_can_be_playing(tmp_path: Path, live_engine: str) -> None:
    """[if] the verified engine has no open performance page (409) [then] None."""
    _write_lock(tmp_path, port=int(live_engine))
    assert maintenance._cli_live_ui_mirror(tmp_path) is None


@pytest.mark.parametrize("live_engine", [(200, {"decks": {"1": {"playing": True}}})], indirect=True)
def test_cli_sync_defers_for_a_playing_deck_seen_on_a_live_engine(
    tmp_path: Path, live_engine: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a live engine's mirror shows a playing deck [then] the CLI sync
    command defers with deck_playing, never reaching the hub."""
    _write_lock(tmp_path, port=int(live_engine))
    exit_code = maintenance.main(
        [
            "sync",
            "--data-dir",
            str(tmp_path),
            # Deliberately unreachable: proves refuse_sync_round short-circuits
            # before any hub I/O, per apps/sync_hub/maintenance.py's sync()
            # docstring contract.
            "--hub",
            "http://127.0.0.1:1",
        ]
    )
    assert exit_code == maintenance.EXIT_SYNC_DEFERRED
    assert "DEFERRED: deck_playing" in capsys.readouterr().err


def test_cli_live_mirror_probe_tolerates_unreachable_engine(tmp_path: Path) -> None:
    """[if] the lock names a port nothing answers on [then] the probe returns
    None rather than raising -- a crashed engine cannot have a playing deck.

    ``resolve_verified_origin`` itself does the identity health check and
    raises ``EngineNotRunning`` (a subclass) for a transport failure there,
    so this exercises that path, not the later ui-mirror GET.
    """
    _write_lock(tmp_path, port=_free_port())  # nothing is bound to this port
    assert maintenance._cli_live_ui_mirror(tmp_path) is None
