"""CLOUDSYNC-14: the standalone CLI must see a live engine's playing deck.

Found by review on PR #3831 (Codex, Tue 22 Sep 2026): ``python -m
apps.sync_hub sync`` always called ``sync(..., ui_mirror=None)``, so
``refuse_sync_round`` could never detect ``any_deck_playing`` from the CLI --
only the in-process HTTP route (which reads ``Request.app.state.ui_mirror``)
honored it. An operator running the CLI by hand while a real engine has a
deck playing would defer only for Gig posture, never for a playing deck,
contradicting the shipped CLOUDSYNC-14 requirement text verbatim.

A second review round (Sol + Codex, Wed 24 Sep 2026) found the fix itself was
still fail-open in two ways, both closed here too: ``httpx.ConnectError`` was
read as "safely absent" for ANY host, not just a verified loopback refusal
(see `_refused_by_loopback_engine`); and a 200 ui-mirror body was trusted
without checking whether it was fresh enough to describe NOW (see
`_ui_mirror_is_fresh`). The same round also required the acceptance tests
below to exercise the REAL production engine (`apps.engine_core.app.
create_app`) over real HTTP rather than a hand-written stand-in, since this
suite is the evidence CLOUDSYNC-14 shipped.

A third round (Codex, Fri 25 Sep 2026) found four hand-rolled FastAPI
"engine" stand-ins still standing after the second round's fix. Two remain
by design and stay labeled as such where they are defined
(`test_409_with_unexpected_body_is_inconclusive_not_safe`,
`test_malformed_200_body_fails_closed_not_crashes`): the real ui-mirror route
cannot emit either byte sequence at all (a hardcoded 409 body, or non-JSON
for a 200), so these exercise `_probe_engine_lock`'s OWN defensive parsing,
not a claim about production behavior. The other two -- a hand-rolled
``/api/v1/health`` that slept in its handler, and a hand-rolled health PLUS
ui-mirror pair where the second slept -- were genuinely replaceable and are
now `start_hanging_listener` (a bare TCP listener with no API surface at
all) and `start_path_delaying_proxy` (a raw byte relay in front of the REAL
engine that delays one path's bytes and fabricates nothing), respectively.

[if] a live engine's mirror shows a playing deck [then] the CLI sync defers, [else stop].
[if] no engine lock file exists at --data-dir [then] the CLI mirror probe returns None, [else stop].
[if] a live engine has no open page (409) [then] the CLI mirror probe returns None, [else stop].
[if] a verified engine answers a non-200/409 status [then] the probe raises an error, [else stop].
[if] a verified engine's mirror request times out [then] the probe raises an error, [else stop].
[if] a verified 409 body is not exactly client_open false [then] the probe raises, [else stop].
[if] a verified engine's 200 body is not JSON [then] the probe raises, not crashes, [else stop].
[if] a non-loopback host refuses the connection [then] the probe still defers, [else stop].
[if] a verified engine's 200 body is stale [then] the probe defers, not trusts it, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from apps.shared import engine_origin
from apps.shared.sync_runtime_gates import SyncDeferredError, any_deck_playing
from apps.sync_hub import maintenance
from tests.cloudsync.live_engine_rig import (
    BOOT_ID,
    boot_real_engine,
    free_port,
    start_hanging_listener,
    start_path_delaying_proxy,
    write_lock,
)
from tests.waits import start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("CLOUDSYNC-14")


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


@pytest.fixture
def real_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, Path, FastAPI]]:
    """One real engine on its own data dir. Yields ``(base_url, data_dir,
    app)`` -- ``app`` is only for the rare test that must reach into real
    ``app.state`` directly (see `test_verified_engine_error_status_is_inconclusive_not_safe`).
    """
    data_dir = tmp_path / "engine-data"
    port = free_port()
    app, server, thread, lock = boot_real_engine(data_dir, monkeypatch, port=port)
    try:
        yield f"http://127.0.0.1:{port}", data_dir, app
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        lock.release()


def test_no_lock_file_means_nothing_can_be_playing(tmp_path: Path) -> None:
    """[if] no engine lock file [then] the CLI mirror probe returns None."""
    assert maintenance._cli_live_ui_mirror(tmp_path) is None


def test_live_engine_mirror_is_read_when_verified(
    real_engine: tuple[str, Path, FastAPI],
) -> None:
    """[if] a verified live engine answers 200 [then] its mirror body is returned."""
    base_url, data_dir, _app = real_engine
    put_response = httpx.put(
        f"{base_url}/api/v1/state/ui-mirror",
        json={"decks": {"1": {"playing": True}}},
        timeout=5.0,
    )
    assert put_response.status_code == 202
    mirror = maintenance._cli_live_ui_mirror(data_dir)
    assert mirror is not None
    assert mirror["decks"]["1"]["playing"] is True
    assert any_deck_playing(mirror)


def test_no_open_page_means_nothing_can_be_playing(
    real_engine: tuple[str, Path, FastAPI],
) -> None:
    """[if] the verified engine has no open performance page (409) [then] None."""
    _base_url, data_dir, _app = real_engine
    assert maintenance._cli_live_ui_mirror(data_dir) is None


def test_cli_sync_defers_for_a_playing_deck_seen_on_a_live_engine(
    real_engine: tuple[str, Path, FastAPI], capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a live engine's mirror shows a playing deck [then] the CLI sync
    command defers with deck_playing, never reaching the hub."""
    base_url, data_dir, _app = real_engine
    put_response = httpx.put(
        f"{base_url}/api/v1/state/ui-mirror",
        json={"decks": {"1": {"playing": True}}},
        timeout=5.0,
    )
    assert put_response.status_code == 202
    exit_code = maintenance.main(
        [
            "sync",
            "--data-dir",
            str(data_dir),
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

    ``verify_engine_identity`` itself does the identity health check and
    raises ``EngineNotRunning`` (a subclass) for a transport failure there,
    so this exercises that path, not the later ui-mirror GET.
    """
    write_lock(tmp_path, port=free_port())  # nothing is bound to this port
    assert maintenance._cli_live_ui_mirror(tmp_path) is None


def test_non_loopback_host_refusing_a_connection_still_defers(tmp_path: Path) -> None:
    """[if] the lock names a NON-loopback host that refuses the connection
    [then] the probe still defers, never reads it as safely absent (Sol
    review, PR #3831, P1/BLOCKING).

    ``"127.1"`` is accepted by the OS resolver as shorthand for
    ``127.0.0.1`` (BSD ``inet_aton`` semantics), so it refuses a connection
    to an unbound port exactly like ``"127.0.0.1"`` does -- genuinely,
    deterministically, with no network dependency -- while not being one of
    the exact spellings `_LOOPBACK_HOSTS` trusts. Before this fix,
    ``isinstance(exc.__cause__, httpx.ConnectError)`` alone would have read
    ANY refused connection as "safely absent" regardless of host; a lock
    naming a real non-loopback host that hit a DNS failure or an unreachable
    network would have been waved through the exact same way.
    """
    write_lock(tmp_path, port=free_port(), host="127.1")
    with pytest.raises(SyncDeferredError) as excinfo:
        maintenance._cli_live_ui_mirror(tmp_path)
    assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE


def test_verified_engine_error_status_is_inconclusive_not_safe(
    real_engine: tuple[str, Path, FastAPI],
) -> None:
    """A genuine server error must not be read as "nothing is playing"
    (claude-review, PR #3831, P1/BLOCKING): only 200 and the documented 409
    are conclusive: everything else fails closed.

    Forces the REAL route's own type-check invariant
    (``apps/webui/server/routes/state.py``'s ``_mirror_store``) rather than a
    hand-rolled fake status code: ``app.state.ui_mirror`` is set directly to
    a value that is not a JSON object, which is exactly the corrupted-state
    case that check exists to catch, and the real ASGI stack turns the
    resulting uncaught ``TypeError`` into a genuine 500 over real HTTP.
    """
    _base_url, data_dir, app = real_engine
    app.state.ui_mirror = ["not", "a", "mapping"]
    with pytest.raises(SyncDeferredError) as excinfo:
        maintenance._cli_live_ui_mirror(data_dir)
    assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE


def test_409_with_unexpected_body_is_inconclusive_not_safe(
    tmp_path: Path,
) -> None:
    """A 409 whose body is not exactly the documented {"client_open": false}
    must not be waved through as safe (claude-review, PR #3831, P3): some
    other conflict could return 409 too, and only the documented shape is a
    verified "no open page".

    The real ``ui-mirror`` route's 409 body is a hardcoded literal
    (``apps/webui/server/routes/state.py``'s ``get_ui_mirror``) -- it cannot
    genuinely serve any other 409 shape, so this specific adversarial shape
    can only be produced by a minimal stand-in. Unlike `_fake_engine_app`
    (removed: Codex review, PR #3831, P1/BLOCKING), this exercises
    `_probe_engine_lock`'s OWN generic defensive parsing against a byte
    sequence the real engine cannot emit, not a claim about production
    engine behavior -- the same category as
    `test_malformed_200_body_fails_closed_not_crashes` below.
    """
    app = FastAPI()

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"boot_id": BOOT_ID}

    @app.get("/api/v1/state/ui-mirror")
    def ui_mirror() -> JSONResponse:
        return JSONResponse(status_code=409, content={"client_open": True})

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the unexpected-409 fake engine")
    try:
        write_lock(tmp_path, port=port)
        with pytest.raises(SyncDeferredError) as excinfo:
            maintenance._cli_live_ui_mirror(tmp_path)
        assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def test_malformed_200_body_fails_closed_not_crashes(tmp_path: Path) -> None:
    """A 200 whose body is not valid JSON must raise SyncDeferredError, never
    an uncaught JSONDecodeError out of a sync command (claude-review, PR
    #3831, P3)."""
    app = FastAPI()

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"boot_id": BOOT_ID}

    @app.get("/api/v1/state/ui-mirror")
    def ui_mirror() -> PlainTextResponse:
        return PlainTextResponse("not json", status_code=200)

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the malformed-body fake engine")
    try:
        write_lock(tmp_path, port=port)
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
    this exact case, and only a VERIFIED loopback ``ConnectionRefusedError``
    cause may read as safe. This exercises the identity probe specifically --
    the earlier of the two HTTP calls, and a distinct code path from
    ``test_verified_engine_mirror_timeout_fails_closed`` above, which hangs
    the later ui-mirror GET instead.

    Uses a bare hanging TCP listener, not a hand-rolled FastAPI health route
    (Codex review, PR #3831, P1/BLOCKING: the prior fake constructed a real
    ``/api/v1/health`` endpoint and slept inside its handler, which is a fake
    API surface this repo's "never mock APIs" rule exists to catch even
    though the sleep meant it was never actually reached). Only the identity
    probe runs before this test's assertion, so a listener that answers
    nothing at all is a complete, faithful stand-in: the client's request
    times out identically whether the silence comes from a stalled real
    engine or from nothing implementing HTTP on the other end.
    """
    monkeypatch.setattr(engine_origin, "IDENTITY_PROBE_TIMEOUT_S", 0.2)
    port = free_port()
    listener, thread = start_hanging_listener(port=port)
    try:
        write_lock(tmp_path, port=port)
        with pytest.raises(SyncDeferredError) as excinfo:
            maintenance._cli_live_ui_mirror(tmp_path)
        assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE
        # The deferral must come from a real client TIMEOUT, not an EOF or
        # protocol error (Sol, PR #3831, P1/BLOCKING, review comment
        # 4108494701): otherwise this test keeps passing if timeout handling
        # regresses to fail-open while connection-close handling stays closed.
        chain = []
        cause: BaseException | None = excinfo.value
        while cause is not None:
            chain.append(cause)
            cause = cause.__cause__ or cause.__context__
        assert any(isinstance(link, httpx.TimeoutException) for link in chain), [
            type(link).__name__ for link in chain
        ]
    finally:
        listener.close()
        thread.join(timeout=10.0)


def _run_two_engine_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    override_playing: bool,
    data_dir_playing: bool,
) -> Mapping[str, Any] | None:
    """Stand up TWO REAL engines (Codex review, PR #3831, P1/BLOCKING: no
    hand-rolled stand-in), one per candidate lock, and return what
    ``_cli_live_ui_mirror`` reports against ``data_dir`` with
    ``OPENDJ_LIVE_LOCK_PATH`` pointed at the other one.
    """
    override_data_dir = tmp_path / "sandboxed-engine"
    data_dir = tmp_path / "unrelated-data-dir"
    override_port = free_port()
    data_dir_port = free_port()
    _override_app, override_server, override_thread, override_lock = boot_real_engine(
        override_data_dir, monkeypatch, port=override_port
    )
    _data_dir_app, data_dir_server, data_dir_thread, data_dir_lock = boot_real_engine(
        data_dir, monkeypatch, port=data_dir_port
    )
    try:
        if override_playing:
            resp = httpx.put(
                f"http://127.0.0.1:{override_port}/api/v1/state/ui-mirror",
                json={"decks": {"1": {"playing": True}}},
                timeout=5.0,
            )
            assert resp.status_code == 202
        if data_dir_playing:
            resp = httpx.put(
                f"http://127.0.0.1:{data_dir_port}/api/v1/state/ui-mirror",
                json={"decks": {"1": {"playing": True}}},
                timeout=5.0,
            )
            assert resp.status_code == 202

        monkeypatch.setenv(engine_origin.LOCK_PATH_ENV, str(override_data_dir / ".engine.lock"))
        return maintenance._cli_live_ui_mirror(data_dir)
    finally:
        override_server.should_exit = True
        override_thread.join(timeout=10.0)
        override_lock.release()
        data_dir_server.should_exit = True
        data_dir_thread.join(timeout=10.0)
        data_dir_lock.release()


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
        tmp_path, monkeypatch, override_playing=True, data_dir_playing=False
    )
    assert mirror is not None
    assert any_deck_playing(mirror)


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
        tmp_path, monkeypatch, override_playing=False, data_dir_playing=True
    )
    assert mirror is not None
    assert any_deck_playing(mirror)


def test_verified_engine_mirror_timeout_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified-live engine that stops answering mid-probe must fail closed,
    not fail open (claude-review, PR #3831, P1/BLOCKING): the whole point of
    this gate is to catch an engine wedged during a live set, so treating its
    silence as "not playing" is exactly backwards.

    Routes through a raw byte-forwarding proxy in front of the REAL engine
    (Codex review, PR #3831, P1/BLOCKING; replaces a hand-rolled FastAPI
    stand-in that implemented both the health AND the ui-mirror route by
    hand). The identity probe's health request is forwarded to, and answered
    verbatim by, the real `create_app` engine; only the ui-mirror request is
    held before being forwarded, long enough to exceed the patched client
    timeout. Every byte either request sees for a real answer came from the
    real engine -- the proxy adds a delay to one path, nothing else, which
    is the one thing needed here that a real engine's own request handling
    gives no test seam for without adding a test-only delay to production
    code itself. See `start_path_delaying_proxy`.
    """
    monkeypatch.setattr(maintenance, "_LIVE_MIRROR_PROBE_TIMEOUT_S", 0.2)
    data_dir = tmp_path / "engine-data"
    engine_port = free_port()
    _app, server, engine_thread, lock = boot_real_engine(data_dir, monkeypatch, port=engine_port)
    proxy_listener, proxy_thread, proxy_port = start_path_delaying_proxy(
        upstream_port=engine_port,
        delay_path="/api/v1/state/ui-mirror",
        delay_s=1.0,  # exceeds the patched 0.2s client timeout
    )
    try:
        lock_file = data_dir / ".engine.lock"
        lock_payload = json.loads(lock_file.read_text(encoding="utf-8"))
        lock_payload["port"] = proxy_port  # route the probe through the proxy
        lock_file.write_text(json.dumps(lock_payload), encoding="utf-8")
        with pytest.raises(SyncDeferredError) as excinfo:
            maintenance._cli_live_ui_mirror(data_dir)
        assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE
    finally:
        proxy_listener.close()
        proxy_thread.join(timeout=10.0)
        server.should_exit = True
        engine_thread.join(timeout=10.0)
        lock.release()


# ----- CLOUDSYNC-14 round 2, finding 2: stale ui-mirror bodies -------------


def test_stale_real_mirror_defers_even_though_status_is_200(
    real_engine: tuple[str, Path, FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a VERIFIED engine's 200 ui-mirror body is older than the
    freshness tolerance [then] the probe defers rather than trusting it
    (Codex review, PR #3831, P1/BLOCKING): the real route serves its last
    stored document indefinitely without re-stamping it on GET, so a 200
    alone is not proof the document describes NOW -- if the page's main
    thread stalls after starting playback but before its next 1 s publish,
    the route keeps serving the last (idle) snapshot while the deck plays on.

    Tightens the real tolerance to 0s (the same technique already used in
    this file for the probe timeouts) rather than faking a stale response,
    so the ``received_at`` this reads back is the REAL server-stamped one
    from a REAL PUT, not a synthetic one.
    """
    base_url, data_dir, _app = real_engine
    put_response = httpx.put(
        f"{base_url}/api/v1/state/ui-mirror",
        json={"decks": {"1": {"playing": False}}},
        timeout=5.0,
    )
    assert put_response.status_code == 202
    monkeypatch.setattr(maintenance, "_UI_MIRROR_FRESHNESS_TOLERANCE_S", 0.0)
    with pytest.raises(SyncDeferredError) as excinfo:
        maintenance._cli_live_ui_mirror(data_dir)
    assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE


# ----- CLOUDSYNC-14 round 3 (Sol review 5323846238): proxies, incomplete docs -


def test_environment_proxy_does_not_carry_the_engine_probe_elsewhere(
    real_engine: tuple[str, Path, FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the shell exports an HTTP proxy that refuses every connection
    [then] the probe still reaches the engine its lock names and sees the
    playing deck (Sol review, PR #3831, P1/BLOCKING). httpx honors proxy
    variables by default, and a refused hop through a LOOPBACK proxy is
    indistinguishable from a stopped engine, which reads as "nothing can be
    playing"."""
    base_url, data_dir, _app = real_engine
    put_response = httpx.put(
        f"{base_url}/api/v1/state/ui-mirror",
        json={"decks": {"1": {"playing": True}}},
        timeout=5.0,
    )
    assert put_response.status_code == 202
    refusing_proxy = f"http://127.0.0.1:{free_port()}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, refusing_proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    mirror = maintenance._cli_live_ui_mirror(data_dir)
    assert mirror is not None and any_deck_playing(mirror)


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"decks": {}},
        {"decks": [{"playing": True}]},
        {"decks": {"1": {}}},
        {"decks": {"1": {"playing": "true"}}},
        {"decks": {"1": {"playing": False}, "2": "playing"}},
    ],
    ids=["no-decks", "empty-decks", "decks-list", "deck-no-playing", "playing-str", "deck-str"],
)
def test_fresh_mirror_without_readable_decks_defers(
    real_engine: tuple[str, Path, FastAPI], document: dict[str, Any]
) -> None:
    """[if] a verified engine serves a FRESH 200 mirror whose decks the gate
    cannot read [then] the probe defers (Sol review, PR #3831, P1/BLOCKING):
    ``any_deck_playing`` reads a missing or malformed deck as not playing."""
    base_url, data_dir, _app = real_engine
    put_response = httpx.put(f"{base_url}/api/v1/state/ui-mirror", json=document, timeout=5.0)
    assert put_response.status_code == 202
    with pytest.raises(SyncDeferredError) as excinfo:
        maintenance._cli_live_ui_mirror(data_dir)
    assert excinfo.value.reason == maintenance.DEFER_REASON_ENGINE_MIRROR_UNREACHABLE


def test_fresh_complete_idle_mirror_still_reads_as_not_playing(
    real_engine: tuple[str, Path, FastAPI],
) -> None:
    """Control for the test above, the other direction: a complete idle
    four-deck document is trusted and reads as nothing playing, so the
    structure check refuses only what it cannot read."""
    base_url, data_dir, _app = real_engine
    idle = {"decks": {str(n): {"playing": False, "effective_bpm": 124.0} for n in range(1, 5)}}
    put_response = httpx.put(f"{base_url}/api/v1/state/ui-mirror", json=idle, timeout=5.0)
    assert put_response.status_code == 202
    mirror = maintenance._cli_live_ui_mirror(data_dir)
    assert mirror is not None and not any_deck_playing(mirror)
