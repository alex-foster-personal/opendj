"""CLOUDSYNC-14: the standalone CLI must see a live engine's playing deck.

Found by review on PR #3831 (Codex, Tue 22 Sep 2026): ``python -m
apps.sync_hub sync`` always called ``sync(..., ui_mirror=None)``, so
``refuse_sync_round`` could never detect ``any_deck_playing`` from the CLI --
only the in-process HTTP route (which reads ``Request.app.state.ui_mirror``)
honored it. An operator running the CLI by hand while a real engine has a
deck playing would defer only for Gig posture, never for a playing deck,
contradicting the shipped CLOUDSYNC-14 requirement text verbatim.

[if] a live engine's mirror shows a playing deck [then] the CLI sync defers, [else stop].
[if] no engine lock file exists at --data-dir [then] the CLI mirror probe returns None, [else stop].
[if] a live engine has no open page (409) [then] the CLI mirror probe returns None, [else stop].
[if] a verified engine answers a non-200/409 status [then] the probe raises an error, [else stop].
[if] a verified engine's mirror request times out [then] the probe raises an error, [else stop].
[if] a verified 409 body is not exactly client_open false [then] the probe raises, [else stop].
[if] a verified engine's 200 body is not JSON [then] the probe raises, not crashes, [else stop].
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from apps.shared import engine_origin
from apps.shared.sync_runtime_gates import SyncDeferredError
from apps.sync_hub import maintenance
from apps.webui.server.routes import state as ui_mirror_routes
from tests.waits import start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("CLOUDSYNC-14")

_BOOT_ID = "test-boot-cloudsync14"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def _no_stray_lock_path_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here that expects the data-dir lock to be used must not be
    at the mercy of whatever the calling shell happens to export
    (claude-review, PR #3831, P3): a developer or agent running this suite
    from a live-debug shell with ``OPENDJ_LIVE_LOCK_PATH`` already set would
    otherwise have those tests probe a real engine instead of the fixture,
    making results depend on that engine's state rather than on the code
    under test. The one test that deliberately sets the override does so
    itself, after this autouse fixture has already cleared it.
    """
    monkeypatch.delenv(engine_origin.LOCK_PATH_ENV, raising=False)


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


def test_fake_engine_409_body_matches_the_real_ui_mirror_route() -> None:
    """Ties the fake engine's 409 fixture to the real route's actual output
    (claude-review, PR #3831, P3/NON-BLOCKING): a real-route change that
    silently returned a different "closed" shape would otherwise leave the
    fake fixture, and this whole test module, quietly divorced from
    production behavior. ``TestClient`` never binds a socket -- it drives
    the real ASGI app in-process, which is exactly the same
    ``apps.webui.server.routes.state`` module the live engine serves.
    """
    app = FastAPI()
    app.include_router(ui_mirror_routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.get("/api/v1/state/ui-mirror")
    assert response.status_code == 409
    assert response.json() == {"client_open": False}


def _real_playing_deck_mirror_body() -> dict[str, Any]:
    """The EXACT 200 body the real ui-mirror route serves for a playing deck.

    Ties the "playing deck" fixture to the real route's actual output
    (claude-review, PR #3831, flagged across three review rounds): a
    hand-written ``{"decks": {"1": {"playing": True}}}`` could silently drift
    from what ``publish_ui_mirror``/``get_ui_mirror`` actually wrap it in
    (``received_at`` is added server-side), leaving the fixtures below --
    and the shipped claim they back -- divorced from production. This PUTs a
    playing mirror through the real router and GETs it back, exactly like
    ``test_fake_engine_409_body_matches_the_real_ui_mirror_route`` already
    does for the closed-page case.
    """
    app = FastAPI()
    app.include_router(ui_mirror_routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        put_response = client.put(
            "/api/v1/state/ui-mirror", json={"decks": {"1": {"playing": True}}}
        )
        assert put_response.status_code == 202
        get_response = client.get("/api/v1/state/ui-mirror")
    assert get_response.status_code == 200
    return get_response.json()


#: Computed once, at collection time, so every fixture and assertion below
#: reads the SAME real-route body rather than a fresh (differently
#: timestamped) round trip per use.
_PLAYING_DECK_MIRROR_BODY = _real_playing_deck_mirror_body()


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


@pytest.mark.parametrize("live_engine", [(200, _PLAYING_DECK_MIRROR_BODY)], indirect=True)
def test_live_engine_mirror_is_read_when_verified(tmp_path: Path, live_engine: str) -> None:
    """[if] a verified live engine answers 200 [then] its mirror body is returned."""
    _write_lock(tmp_path, port=int(live_engine))
    mirror = maintenance._cli_live_ui_mirror(tmp_path)
    assert mirror == _PLAYING_DECK_MIRROR_BODY


@pytest.mark.parametrize("live_engine", [(409, {"client_open": False})], indirect=True)
def test_no_open_page_means_nothing_can_be_playing(tmp_path: Path, live_engine: str) -> None:
    """[if] the verified engine has no open performance page (409) [then] None."""
    _write_lock(tmp_path, port=int(live_engine))
    assert maintenance._cli_live_ui_mirror(tmp_path) is None


@pytest.mark.parametrize("live_engine", [(200, _PLAYING_DECK_MIRROR_BODY)], indirect=True)
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


@pytest.mark.parametrize("live_engine", [(500, {"error": "boom"})], indirect=True)
def test_verified_engine_error_status_is_inconclusive_not_safe(
    tmp_path: Path, live_engine: str
) -> None:
    """A genuine server error must not be read as "nothing is playing"
    (claude-review, PR #3831, P1/BLOCKING): only 200 and the documented 409
    are conclusive: everything else fails closed."""
    _write_lock(tmp_path, port=int(live_engine))
    with pytest.raises(SyncDeferredError) as excinfo:
        maintenance._cli_live_ui_mirror(tmp_path)
    assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE


@pytest.mark.parametrize("live_engine", [(409, {"client_open": True})], indirect=True)
def test_409_with_unexpected_body_is_inconclusive_not_safe(
    tmp_path: Path, live_engine: str
) -> None:
    """A 409 whose body is not exactly the documented {"client_open": false}
    must not be waved through as safe (claude-review, PR #3831, P3): some
    other conflict could return 409 too, and only the documented shape is a
    verified "no open page"."""
    _write_lock(tmp_path, port=int(live_engine))
    with pytest.raises(SyncDeferredError) as excinfo:
        maintenance._cli_live_ui_mirror(tmp_path)
    assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE


def test_malformed_200_body_fails_closed_not_crashes(tmp_path: Path) -> None:
    """A 200 whose body is not valid JSON must raise SyncDeferredError, never
    an uncaught JSONDecodeError out of a sync command (claude-review, PR
    #3831, P3)."""
    app = FastAPI()

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"boot_id": _BOOT_ID}

    @app.get("/api/v1/state/ui-mirror")
    def ui_mirror() -> PlainTextResponse:
        return PlainTextResponse("not json", status_code=200)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the malformed-body fake engine")
    try:
        _write_lock(tmp_path, port=port)
        with pytest.raises(SyncDeferredError) as excinfo:
            maintenance._cli_live_ui_mirror(tmp_path)
        assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def test_locked_engine_health_check_timeout_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked-but-wedged engine (accepts the connection, then hangs on
    ``/api/v1/health``) must fail closed, not read as "nothing is playing"
    (claude-review, PR #3831, P1/BLOCKING): ``verify_engine_identity`` raises
    ``EngineNotRunning`` `from` the underlying ``httpx.TimeoutException`` for
    this exact case, and only a ``ConnectError`` cause (a port that refused
    the connection outright) may read as safe. This exercises the identity
    probe specifically -- the earlier of the two HTTP calls, and a distinct
    code path from ``test_verified_engine_mirror_timeout_fails_closed``
    above, which hangs the later ui-mirror GET instead.
    """
    monkeypatch.setattr(engine_origin, "IDENTITY_PROBE_TIMEOUT_S", 0.2)
    app = FastAPI()

    @app.get("/api/v1/health")
    async def health() -> dict:
        await asyncio.sleep(1.0)  # exceeds the patched 0.2s client timeout
        return {"boot_id": _BOOT_ID}

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the health-hanging fake engine")
    try:
        _write_lock(tmp_path, port=port)
        with pytest.raises(SyncDeferredError) as excinfo:
            maintenance._cli_live_ui_mirror(tmp_path)
        assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def _run_two_engine_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    override_body: dict[str, Any],
    data_dir_body: dict[str, Any],
) -> Mapping[str, Any] | None:
    """Stand up TWO distinct live fake engines, one per candidate lock, and
    return what ``_cli_live_ui_mirror`` reports against ``data_dir`` with
    ``OPENDJ_LIVE_LOCK_PATH`` pointed at the other one.
    """
    override_app = _fake_engine_app(200, override_body)
    override_port = _free_port()
    override_config = uvicorn.Config(
        override_app, host="127.0.0.1", port=override_port, log_level="warning"
    )
    override_server, override_thread = start_uvicorn_in_thread(
        override_config, what="the override-lock fake engine"
    )

    data_dir_app = _fake_engine_app(200, data_dir_body)
    data_dir_port = _free_port()
    data_dir_config = uvicorn.Config(
        data_dir_app, host="127.0.0.1", port=data_dir_port, log_level="warning"
    )
    data_dir_server, data_dir_thread = start_uvicorn_in_thread(
        data_dir_config, what="the data-dir-lock fake engine"
    )
    try:
        override_lock = tmp_path / "sandboxed-engine" / ".engine.lock"
        _write_lock(override_lock.parent, port=override_port)
        monkeypatch.setenv(engine_origin.LOCK_PATH_ENV, str(override_lock))

        data_dir = tmp_path / "unrelated-data-dir"
        _write_lock(data_dir, port=data_dir_port)

        return maintenance._cli_live_ui_mirror(data_dir)
    finally:
        override_server.should_exit = True
        override_thread.join(timeout=10.0)
        data_dir_server.should_exit = True
        data_dir_thread.join(timeout=10.0)


def test_opendj_live_lock_path_overrides_playing_deck_is_not_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the OVERRIDE engine has a playing deck and the data-dir engine
    does not [then] the CLI still reports the playing deck (claude-review,
    PR #3831, P2): a naive "override wins outright" would happen to pass
    this direction too, but the OTHER direction below is what would catch
    that regression -- both are needed, per the mutate-the-guard-in-both-
    directions principle.
    """
    mirror = _run_two_engine_probe(
        tmp_path,
        monkeypatch,
        override_body=_PLAYING_DECK_MIRROR_BODY,
        data_dir_body={"decks": {}},
    )
    assert mirror == _PLAYING_DECK_MIRROR_BODY


def test_data_dirs_own_playing_deck_is_not_dropped_for_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the DATA-DIR engine has a playing deck and the override engine
    does not [then] the CLI still reports the playing deck (claude-review,
    PR #3831, P2): this is the direction that actually catches a regression
    to "override wins outright" -- if ``_candidate_lock_files`` ever dropped
    the data-dir lock whenever an override is set, this assertion would fail
    while the previous test's would not.
    """
    mirror = _run_two_engine_probe(
        tmp_path,
        monkeypatch,
        override_body={"decks": {}},
        data_dir_body=_PLAYING_DECK_MIRROR_BODY,
    )
    assert mirror == _PLAYING_DECK_MIRROR_BODY


def test_verified_engine_mirror_timeout_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified-live engine that stops answering mid-probe must fail closed,
    not fail open (claude-review, PR #3831, P1/BLOCKING): the whole point of
    this gate is to catch an engine wedged during a live set, so treating its
    silence as "not playing" is exactly backwards."""
    monkeypatch.setattr(maintenance, "_LIVE_MIRROR_PROBE_TIMEOUT_S", 0.2)
    app = FastAPI()

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"boot_id": _BOOT_ID}

    @app.get("/api/v1/state/ui-mirror")
    async def ui_mirror() -> JSONResponse:
        await asyncio.sleep(1.0)  # exceeds the patched 0.2s client timeout
        return JSONResponse(status_code=200, content={})

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the hanging fake engine")
    try:
        _write_lock(tmp_path, port=port)
        with pytest.raises(SyncDeferredError) as excinfo:
            maintenance._cli_live_ui_mirror(tmp_path)
        assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
