"""A real engine on a real socket, plus the page that claims its orders.

``engine`` serves the production app (AGENT-02's mirror route and AGENT-03's
order broker) over a loopback socket, and :class:`PerformancePage` is a client
of those same routes doing what the browser does: publish a mirror, poll for
the next order, execute it, post the result document back. The CLI then reaches
that engine through the origin it read out of a real lock file. Nothing on the
CLI's side of that boundary is stood in for.

WHAT THIS IS NOT. ``_EXECUTORS`` is a MODEL of how the production mirror answers
a command, not the browser's ``dispatchPerformanceCommand`` and not the DSP
graph. It cannot be: there is no headless audio path (see
``apps/opendj_cli/__init__.py``), the Web Audio engine is browser-owned, and a
pytest process has no page to open. The browser half of the contract is pinned
two other ways - ``test_bus_parity.py`` reads the live TypeScript union that
validates a command on the wire, and the frontend suite owns the dispatcher and
the graph - so a passing test here is evidence about the CLI, never about the
engine.

The risk that buys is a model that agrees with the CLI's own expectations
instead of with production, which is how ``unload`` shipped unconfirmable
(#1739). Every divergence found is corrected against the named production
source, and each executor below cites the source it models.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import httpx
import pytest
import uvicorn
from fastapi import Depends, Request

from apps.engine_core.app import ENGINE_VERSION, HEALTH_PATH, EngineHealthOut, _drop_or_raise
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, StateBackend, Track
from apps.webui.server.deps import get_read_state
from apps.webui.server.routes.health import health as legacy_health
from tests.opendj_cli.updater_rig import updater
from tests.waits import LIVENESS_CHECK_S, THREAD_HANG_GUARD_S, start_uvicorn_in_thread

# Published for pytest: the AGENT-13 updater fixture lives in updater_rig.
__all__ = ["updater"]

POLL_S = 0.02


def deck_mirror() -> dict[str, Any]:
    return {
        # `buildUiMirror` publishes stable_id beside title: title alone cannot
        # say WHICH track landed on an already-loaded deck.
        "stable_id": None,
        "title": None,
        "artist": None,
        "playing": False,
        "audible": False,
        "bpm": 124.0,
        # The PITCHED tempo (`playbackBpm(..., tempoRatio: st.pitch, ...)`), and
        # so the one a beat-relative Duration really travels at. Published per
        # deck by `buildUiMirror`; the CLI sizes a ramp deadline off it.
        "effective_bpm": 124.0,
        "position": {"ms": 0.0, "bars_beats": "1.1", "phrase": 1},
        "pitch": 1.0,
        "presentation_clock": {
            "source": "audio_output",
            "desired_revision": 0,
            "presented_revision": 0,
        },
        "sync": {"mode": "beat", "enabled": False},
        "stems": {
            "status": "unavailable",
            "available_controls": [],
            "controls": {
                "vocal": {"muted": False, "solo": False, "gain": 0.5},
                "instrumental": {"muted": False, "solo": False, "gain": 0.5},
                "drums": {"muted": False, "solo": False, "gain": 0.5},
            },
        },
    }


def channel_mirror(deck_id: int) -> dict[str, Any]:
    return {
        "deck_id": deck_id,
        "trim": 0.5,
        "eq_high": 0.5,
        "eq_mid": 0.5,
        "eq_low": 0.5,
        "filter": 0.5,
        "fader": 1.0,
        "assign": "A",
        "cue_enabled": False,
        "stem_eq_mode": False,
    }


def blank_mirror() -> dict[str, Any]:
    return {
        "client_open": True,
        "context_state": "running",
        "master": {"muted": False, "level": 0.8, "rms": 0.02},
        "mixer": {
            "channels": {str(deck): channel_mirror(deck) for deck in (1, 2, 3, 4)},
            "crossfader": 0.5,
        },
        # `buildUiMirror` publishes the elected master at the top level: it is
        # what `_ramp` resolves `clock: master` against
        # (apps/webui/frontend/src/lib/rb/agent-orders.ts), and with no master
        # the page throws `no_master` rather than holding the order open.
        "master_deck": 1,
        "master_mode": "auto",
        "master_reason": None,
        "decks": {str(deck): deck_mirror() for deck in (1, 2, 3, 4)},
        "toasts": [],
    }


def _deck_of(mirror: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    return mirror["decks"][str(command["deck"])]


def _channel_of(mirror: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    return mirror["mixer"]["channels"][str(command["deck"])]


def _load(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    deck = _deck_of(mirror, command)
    deck["stable_id"] = command["stable_id"]
    deck["title"] = f"title of {command['stable_id']}"


def _unload(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    """``engine.unload`` resets the slot to ``_emptyDeckState``: title null.

    Source: ``apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts`` (unload)
    and ``apps/webui/frontend/src/lib/player/state.svelte.ts``
    (``_emptyDeckState``, ``title: null``). The runtime is replaced with
    ``_emptyRuntime()`` too, so the presentation clock restarts at 0 == 0
    rather than advancing - see ``_TRANSPORT_TYPES`` below.
    """
    deck = _deck_of(mirror, command)
    deck["stable_id"] = None
    deck["title"] = None
    deck["playing"] = False
    deck["presentation_clock"] |= {"desired_revision": 0, "presented_revision": 0}


def _tempo(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    """``setTempoRatio`` lands as ``st.pitch = scheduledTempoRatio``.

    Source: ``apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts``.
    """
    deck = _deck_of(mirror, command)
    deck["pitch"] = command["ratio"]
    if deck["bpm"] is not None:
        deck["effective_bpm"] = deck["bpm"] * command["ratio"]


def _play(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    _deck_of(mirror, command)["playing"] = command["playing"]


def _eq(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    _channel_of(mirror, command)[f"eq_{command['band']}"] = command["value"]


def _knob(key: str) -> Any:
    def apply(mirror: dict[str, Any], command: dict[str, Any]) -> None:
        _channel_of(mirror, command)[key] = command["value"]

    return apply


def _assign(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    _channel_of(mirror, command)["assign"] = command["assign"]


def _channel_cue(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    _channel_of(mirror, command)["cue_enabled"] = command["enabled"]


def _stem_eq_mode(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    _channel_of(mirror, command)["stem_eq_mode"] = command["enabled"]


def _stem_gain(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    deck = _deck_of(mirror, command)
    deck["stems"]["controls"][command["stem"]]["gain"] = command["value"]


def _crossfader(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    mirror["mixer"]["crossfader"] = command["value"]


def _master_volume(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    mirror["master"]["level"] = command["value"]


def _master_mute(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    mirror["master"]["muted"] = command["muted"]


def _no_mirror_effect(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    """Applied by production, published by nothing.

    ``buildUiMirror`` (apps/webui/frontend/src/lib/rb/ui-mirror.ts) emits
    title, artist, key, bpm, effective_bpm, position, playing, audible,
    presentation_clock, loop, hot_cues, pitch, sync, stems and phrases per
    deck. ``slip_enabled``, ``quantize_enabled``, ``master_tempo_enabled`` and
    ``key_sync_enabled`` are real deck state that no field carries, so the
    mirror cannot answer for them however well the command ran. That is the
    AGENT-02 gap the ``accepted`` verdict exists to name, and a page that
    silently pretended otherwise would hide it.
    """


def _beat_sync(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    _deck_of(mirror, command)["sync"]["enabled"] = command["enabled"]


def _sync_mode(mirror: dict[str, Any], command: dict[str, Any]) -> None:
    _deck_of(mirror, command)["sync"]["mode"] = command["mode"]


# The presentation clock advances ONLY when a command schedules audio:
# `acknowledgePresentedTransportSchedule` is reached from `_scheduleDeck`, and
# nothing else moves `desired_revision`
# (apps/webui/frontend/src/lib/player/transport/presentation.ts). A knob turn,
# an assign, a sync-mode flip - none of them touch it, so a mirror command
# leaves the clock reading 0 == 0. Bumping it for EVERY deck command, as this
# page first did, made the clock check look like it was affirming something it
# cannot affirm; `unload` resets rather than advances it, so it is not here.
_TRANSPORT_TYPES: frozenset[str] = frozenset({"load", "play", "tempo"})

_EXECUTORS: dict[str, Any] = {
    "load": _load,
    "unload": _unload,
    "play": _play,
    "tempo": _tempo,
    "eq": _eq,
    "trim": _knob("trim"),
    "filter": _knob("filter"),
    "fader": _knob("fader"),
    "assign": _assign,
    "channel_cue": _channel_cue,
    "stem_eq_mode": _stem_eq_mode,
    "stem_gain": _stem_gain,
    "crossfader": _crossfader,
    "master_volume": _master_volume,
    "master_mute": _master_mute,
    "beat_sync": _beat_sync,
    "sync_mode": _sync_mode,
    "slip": _no_mirror_effect,
    "quantize": _no_mirror_effect,
    "master_tempo": _no_mirror_effect,
    "key_sync": _no_mirror_effect,
}


class PerformancePage:
    """The open performance page, driving the engine's real routes.

    ``apply_commands`` is the switch that reproduces the live fault the CLI has
    to survive: a page that claims an order and reports ``succeeded`` without
    the bus state having moved. ``settle`` is set False to reproduce a deck
    whose presentation clock never catches up.

    Every publish is numbered BEFORE its body is serialized and reported under
    ``_publishes`` once the engine answers, so a test waits on the page's own
    signal rather than on a wall-clock poll: a loaded runner that makes the
    thread slow makes the wait slow, never red. A wait fails three distinct
    ways - DEAD (the loop raised), REJECTED (the engine answered a publish
    with something other than 202) and HANG (the thread is alive and no
    publish it started after the wait began was accepted within the guard).
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.mirror = blank_mirror()
        self.orders: list[dict[str, Any]] = []
        self.apply_commands = True
        self.settle = True
        self.fail_every_order = False
        self._registered = False
        self._running = True
        self._thread: threading.Thread | None = None
        self._crash: BaseException | None = None
        self._publishes = threading.Condition()
        self._publishes_started = 0
        self._last_answered = 0
        self._last_accepted = 0
        self._last_status: int | None = None
        self._last_body = ""

    def start(self) -> None:
        baseline = self.publishes_started()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.wait_for_publish_after(baseline, "the page registering its mirror")

    def publishes_started(self) -> int:
        """How many publishes have begun, the baseline for a later wait."""
        with self._publishes:
            return self._publishes_started

    def wait_for_publish_after(self, baseline: int, what: str) -> None:
        """Block until a publish that STARTED after ``baseline`` is accepted.

        Such a publish serialized ``self.mirror`` after the caller read the
        baseline, so every edit the caller made first is in what the engine
        now holds. ``THREAD_HANG_GUARD_S`` is a hang guard, not a budget.
        """
        started = time.monotonic()
        with self._publishes:
            while self._last_accepted <= baseline:
                if self._last_answered > baseline and self._last_status != 202:
                    raise AssertionError(
                        f"REJECTED: {what}: the engine answered a mirror publish with "
                        f"{self._last_status}: {self._last_body}"
                    )
                if self._thread is None or not self._thread.is_alive():
                    raise AssertionError(
                        f"DEAD: {what}: the page thread is not running ({self._crash!r})"
                    )
                elapsed = time.monotonic() - started
                if elapsed > THREAD_HANG_GUARD_S:
                    raise AssertionError(
                        f"HANG: {what}: no publish started after the wait began was "
                        f"accepted within {elapsed:.1f}s (guard {THREAD_HANG_GUARD_S}s; "
                        f"{self._publishes_started} started, {self._last_answered} "
                        f"answered, last status {self._last_status})"
                    )
                self._publishes.wait(timeout=LIVENESS_CHECK_S)

    def stop(self) -> None:
        """Close the page the way its unmount does, so the engine knows it went."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._registered = False
        httpx.delete(f"{self.base_url}/api/v1/state/ui-mirror", timeout=5.0)

    def _loop(self) -> None:
        try:
            while self._running:
                self._publish()
                self._claim()
                time.sleep(POLL_S)
        except BaseException as exc:
            # Kept for the waiter's DEAD message, then re-raised unchanged.
            self._crash = exc
            raise

    def _publish(self) -> None:
        with self._publishes:
            self._publishes_started += 1
            number = self._publishes_started
        response = httpx.put(
            f"{self.base_url}/api/v1/state/ui-mirror", json=self.mirror, timeout=5.0
        )
        with self._publishes:
            self._registered = response.status_code == 202
            self._last_answered = number
            self._last_status = response.status_code
            self._last_body = response.text
            if self._registered:
                self._last_accepted = number
            self._publishes.notify_all()

    def _claim(self) -> None:
        if not self._registered:
            return
        response = httpx.get(f"{self.base_url}/api/v1/commands/next", timeout=5.0)
        if response.status_code == 409:
            self._registered = False
            return
        response.raise_for_status()
        claimed = response.json()
        if claimed is None:
            return
        self.orders.append({key: value for key, value in claimed.items() if key != "id"})
        before = json.dumps(self.mirror, sort_keys=True)
        steps = self._execute(claimed)
        after = json.dumps(self.mirror, sort_keys=True)
        delta = {"changed": {}} if before == after else {"changed": {"mirror": True}}
        httpx.post(
            f"{self.base_url}/api/v1/commands/{claimed['id']}/result",
            json={"steps": steps, "mirror_delta": delta},
            timeout=5.0,
        ).raise_for_status()
        # Republish BEFORE the next poll tick, exactly as the browser does:
        # `pollAgentOrders` calls `republish()` the moment the result POST
        # returns (apps/webui/frontend/src/lib/rb/agent-orders.ts). Leaving it
        # to the 20ms loop left the engine serving the PRE-ORDER mirror for a
        # moment after the CLI was told the order succeeded, so a settle check
        # could pass on a reading from before the command ran. Found by
        # mutation: an `unload` asserting the OLD, wrong expectation still
        # confirmed, because the stale mirror still held the old title.
        self._publish()

    def _execute(self, order: dict[str, Any]) -> list[dict[str, Any]]:
        if self.fail_every_order:
            return [{"status": "failed", "error": "the page refused this order"}]
        kind = order["kind"]
        payload = order["payload"]
        if kind == "single":
            self._apply(payload)
            return [{"status": "succeeded"}]
        if kind == "sequence":
            return [self._one(command) for command in payload]
        if kind == "parallel":
            return [self._one(command) for command in payload]
        if kind == "ramp":
            # A ramp is the page's own loop, and the ONE order production holds
            # open: `_ramp` in agent-orders.ts ticks every 16ms until the plan
            # completes and `executeAgentOrder` awaits it before posting a
            # result, so the CLI waits out the whole declared duration. This
            # page holds for the same wall-clock time where the duration states
            # one (`ms`), then takes the whole step at once - the same end
            # state the browser's interpolation lands on. Beat-relative units
            # are not held: this page has no tempo to resolve them against.
            over = payload["over"]
            if over["unit"] == "ms":
                time.sleep(float(over["n"]) / 1000.0)
            self._apply({**payload["command"], "value": payload["to"]})
            return [{"status": "succeeded"}]
        raise AssertionError(f"unknown order kind {kind}")

    def _one(self, command: dict[str, Any]) -> dict[str, Any]:
        self._apply(command)
        return {"status": "succeeded"}

    def _apply(self, command: dict[str, Any]) -> None:
        if not self.apply_commands:
            return
        handler = _EXECUTORS.get(command["type"])
        if handler is None:
            raise AssertionError(f"the page cannot execute {command['type']}")
        handler(self.mirror, command)
        deck = str(command.get("deck"))
        if command["type"] in _TRANSPORT_TYPES and deck in self.mirror["decks"]:
            clock = self.mirror["decks"][deck]["presentation_clock"]
            clock["desired_revision"] += 1
            if self.settle:
                clock["presented_revision"] = clock["desired_revision"]


class ShellSimulator:
    """The installed desktop shell: poll pending navigate and open Performance."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._running = False
        self._thread: threading.Thread | None = None
        self._page: PerformancePage | None = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._page is not None:
            self._page.stop()
            self._page = None

    def _loop(self) -> None:
        while self._running:
            try:
                response = httpx.get(
                    f"{self.base_url}/api/v1/shell/navigate/pending", timeout=2.0
                )
                if response.status_code != 200:
                    time.sleep(POLL_S)
                    continue
                pending = response.json().get("pending")
                if not pending:
                    time.sleep(POLL_S)
                    continue
                if self._page is None:
                    self._page = PerformancePage(self.base_url)
                    self._page.start()
                httpx.post(
                    f"{self.base_url}/api/v1/shell/navigate/ack",
                    json={"id": pending["id"]},
                    timeout=2.0,
                ).raise_for_status()
            except Exception:
                pass
            time.sleep(POLL_S)


@dataclass
class Engine:
    """The production app served over a loopback socket, with a lock file."""

    base_url: str
    port: int
    lock_path: Path
    app: Any = field(default=None)

    def page(self) -> PerformancePage:
        return PerformancePage(self.base_url)

    def shell(self) -> ShellSimulator:
        return ShellSimulator(self.base_url)


def _add_test_health_route(app: Any, boot_id: str) -> None:
    """Expose engine-style health so lock boot_id verification can pass."""
    _drop_or_raise(app, HEALTH_PATH, "legacy health route")
    app.state.engine_boot_id = boot_id
    app.state.contract_rev = "test-contract-rev"

    @app.get(
        HEALTH_PATH,
        response_model=EngineHealthOut,
        tags=["health"],
        name="engine_health",
    )
    async def engine_health(
        request: Request,
        backend: Annotated[StateBackend, Depends(get_read_state)],
    ) -> EngineHealthOut:
        # #5494 made routes.health.health `async def`; this fixture mirrors
        # apps/engine_core/app.py's own engine_health wrapper (fixed in
        # e8202e746) and had the identical bug: calling the now-async
        # legacy handler without awaiting it returns a coroutine, and
        # `.model_dump()` on that raises AttributeError -- which the CLI
        # then reports as a dead/erroring engine rather than real health
        # data (tests/opendj_cli/test_engine_identity.py, test_cli_*).
        base = await legacy_health(request, backend)
        return EngineHealthOut(
            **base.model_dump(),
            contract_rev=app.state.contract_rev,
            engine_version=ENGINE_VERSION,
            boot_id=app.state.engine_boot_id,
        )


def _run_engine(tmp_path: Path, backend: InMemoryBackend) -> Any:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    app = create_app(
        backend=backend,
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path / "client-errors",
        client_event_log_dir=tmp_path / "client-events",
    )
    _add_test_health_route(app, "test-boot")
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, log_level="warning"), what="the engine", sockets=[listener]
    )
    lock_path = tmp_path / ".engine.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 4242,
                "role": "opendj-engine",
                "host": "127.0.0.1",
                "port": port,
                "boot_id": "test-boot",
            }
        ),
        encoding="utf-8",
    )
    try:
        yield Engine(
            base_url=f"http://127.0.0.1:{port}", port=port, lock_path=lock_path, app=app
        )
    finally:
        # Graceful first, then insist. A test that WEDGES the page leaves the
        # engine still awaiting a future no page will ever complete, and
        # uvicorn's graceful shutdown waits out that connection: measured 10s
        # of teardown per wedged test, four of them, 40s of a 75s suite spent
        # waiting for a server everyone had finished with. Nothing here asserts
        # anything about graceful shutdown, so there is nothing to lose by
        # stopping asking nicely after half a second.
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)


@pytest.fixture
def engine(tmp_path: Path) -> Any:
    yield from _run_engine(tmp_path, InMemoryBackend())


def _seed_large_library(backend: InMemoryBackend, count: int) -> None:
    """``count`` tracks shaped like a real library row (issue #2878 fixture)."""
    created = "2026-04-17T10:00:00.000000Z"
    for i in range(count):
        backend.seed_track(
            Track(
                stable_id=f"trk-{i:06d}",
                title=f"Track Title Number {i:06d} (Extended Mix)",
                artist=f"Artist Collective {i % 733:04d}",
                album=f"Album {i % 211:04d}",
                duration_ms=180_000 + (i % 600) * 1000,
                bpm=float(90 + (i % 60)),
                key=f"{(i % 12) + 1}A",
                rating=(i % 5) + 1,
                tags=["house", "peak-time", f"tag-{i % 40:03d}"],
                notes=f"Imported from rekordbox library export batch {i % 17}",
                created_at=created,
                updated_at=created,
            )
        )


@pytest.fixture
def big_library_engine(tmp_path: Path) -> Any:
    """A real engine over the generated 8500-row library used by this fixture."""
    backend = InMemoryBackend()
    _seed_large_library(backend, 8500)
    yield from _run_engine(tmp_path, backend)
