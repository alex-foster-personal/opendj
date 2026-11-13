"""LIGHT-05: engine feed adapter, the lighting package's only I/O to the audio engine.

Reads the Rust engine's 30 Hz ``state`` websocket (URL and token from ``GET /api/v1/audio-engine``)
and turns each line into a ``FeedBatch``: one ``DeckFrame`` (a ``beat_clock.DeckSample`` plus track
id and analysis) per loaded deck, and a ``MixerSample``. State lines carry no track id, so deck to
track ids come from the daemon's ``GET /api/v1/state/ui-mirror`` (409 = no page open: ids unknown).
``/anlz`` is fetched once per new track id and refetched when the daemon bus publishes
``library.changed`` with kind ``beatgrid_edit`` or ``sections``. A wrong token raises
``EngineFeedAuthError``; any close raises ``EngineFeedDisconnected``. Reconnecting is the runtime's job.
Each analysis also carries the track's lighting profile, built by the injected ``ProfileBuilder``
(the composition root passes ``AutoDjService.profile_from_payloads``; None when absent or it fails).
``DeckFeedBuilder`` turns batches into runtime ``DeckFeed`` rows whose profile is cleared (None) on the first batch after a deck unloads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from apps.autodj.contract import TrackProfile
from apps.lighting.beat_clock import DeckSample
from apps.lighting.runtime import DeckFeed
from apps.lighting.sections import SectionSpan, resolve_sections
from apps.shared import runtime_policy

log = logging.getLogger(__name__)

ENGINE_STATUS_PATH = "/api/v1/audio-engine"
UI_MIRROR_PATH = "/api/v1/state/ui-mirror"
EVENTS_PATH = "/api/v1/events"
ANLZ_PATH = "/api/v1/tracks/{stable_id}/anlz"
ANLZ_POINTS = runtime_policy.ANLZ_POINTS_MIN
MIRROR_POLL_S = 1.0
REFETCH_KINDS = frozenset({"beatgrid_edit", "sections"})
IGNORED_TYPES = frozenset({"hello", "result", "load_failed", "midi_unmapped"})
WS_OPEN_TIMEOUT_S = 5.0


ProfileBuilder = Callable[[Mapping[str, Any], Mapping[str, Any]], TrackProfile]


class EngineFeedError(RuntimeError):
    """Protocol violation or a bad HTTP answer."""


class EngineFeedUnavailable(EngineFeedError):
    """The engine is not running, so it has no socket or token."""


class EngineFeedAuthError(EngineFeedError):
    """The engine refused the token at the handshake (HTTP 401)."""


class EngineFeedDisconnected(EngineFeedError):
    """The engine socket or the events bus closed."""


@dataclass(frozen=True)
class EngineEndpoint:
    ws_url: str
    token: str


@dataclass(frozen=True)
class TrackAnalysis:
    track_id: str
    grid: tuple[dict[str, float | int], ...]
    grid_missing: str | None
    sections: tuple[SectionSpan, ...]
    profile: TrackProfile | None = None


@dataclass(frozen=True)
class DeckFrame:
    sample: DeckSample
    track_id: str | None
    analysis: TrackAnalysis | None
    duration_ms: float
    tempo: float


@dataclass(frozen=True)
class MixerSample:
    crossfader: float
    master_volume: float
    master_rms: float
    master_muted: bool
    faders: Mapping[str, float]
    assigns: Mapping[str, str]


@dataclass(frozen=True)
class FeedBatch:
    state_seq: int
    frame: int
    received_ns: int
    decks: tuple[DeckFrame, ...]
    mixer: MixerSample


# ----------------------------------------------------------------------------- pure helpers


def _req(obj: Mapping[str, Any], key: str, seq: Any) -> Any:
    if key not in obj:
        raise EngineFeedError(f"state line {seq} is missing {key!r}")
    return obj[key]


def parse_state_line(
    line: Mapping[str, Any],
    received_ns: int,
    track_ids: Mapping[str, str | None],
    analyses: Mapping[str, TrackAnalysis],
) -> FeedBatch:
    """One batch from one engine ``state`` line; loaded decks only, in deck order."""
    seq = line.get("state_seq")
    heard_in = _req(line, "heard_in_ns", seq)
    host_ns = received_ns + heard_in if isinstance(heard_in, int) else received_ns
    frames: list[DeckFrame] = []
    faders: dict[str, float] = {}
    assigns: dict[str, str] = {}
    for deck in _req(line, "decks", seq):
        if not _req(deck, "loaded", seq):
            continue
        deck_id = str(_req(deck, "deck", seq))
        track_id = track_ids.get(deck_id)
        sample = DeckSample(
            deck=deck_id,
            position_ms=float(_req(deck, "position_ms", seq)),
            rate=float(_req(deck, "rate", seq)),
            playing=bool(_req(deck, "playing", seq)),
            host_time_ns=host_ns,
        )
        analysis = analyses.get(track_id) if track_id else None
        frames.append(
            DeckFrame(
                sample, track_id, analysis, float(_req(deck, "duration_ms", seq)), float(_req(deck, "tempo", seq))
            )
        )
        faders[deck_id] = float(_req(deck, "fader", seq))
        assigns[deck_id] = str(_req(deck, "assign", seq))
    mixer, master = _req(line, "mixer", seq), _req(line, "master", seq)
    return FeedBatch(
        state_seq=int(_req(line, "state_seq", seq)),
        frame=int(_req(line, "frame", seq)),
        received_ns=received_ns,
        decks=tuple(sorted(frames, key=lambda f: int(f.sample.deck))),
        mixer=MixerSample(
            crossfader=float(_req(mixer, "crossfader", seq)),
            master_volume=float(_req(mixer, "master_volume", seq)),
            master_rms=float(_req(master, "rms", seq)),
            master_muted=bool(_req(master, "muted", seq)),
            faders=faders,
            assigns=assigns,
        ),
    )


def grid_from_anlz_beatgrid(
    beatgrid: Mapping[str, Any] | None,
) -> tuple[tuple[dict[str, float | int], ...], str | None]:
    """The anlz ``beatgrid`` (seconds) as beat_clock grid beats ``{n, time_ms, bpm?}``, or a missing reason."""
    if not beatgrid:
        return (), "the anlz payload has no beatgrid block"
    raw = beatgrid.get("beats") or []
    if not raw:
        return (), str(beatgrid.get("reason") or f"the {beatgrid.get('source')} beatgrid has no beats")
    beats: list[dict[str, float | int]] = []
    last = float("-inf")
    for b in raw:
        t_ms = float(b["t"]) * 1000.0
        if t_ms <= last:
            raise EngineFeedError(f"{beatgrid.get('source')} beatgrid times do not increase at {t_ms:.3f} ms")
        last = t_ms
        beat: dict[str, float | int] = {"n": int(b["n"]), "time_ms": t_ms}
        if b.get("bpm") is not None:
            beat["bpm"] = float(b["bpm"])
        beats.append(beat)
    return tuple(beats), None


def analysis_from_anlz(
    track_id: str, payload: Mapping[str, Any], duration_ms: float, profile_builder: ProfileBuilder | None = None
) -> TrackAnalysis:
    grid, missing = grid_from_anlz_beatgrid(payload.get("beatgrid"))
    sections = tuple(resolve_sections(payload, duration_ms / 1000.0))
    return TrackAnalysis(track_id, grid, missing, sections, profile_from_anlz(track_id, payload, profile_builder))


def profile_from_anlz(
    track_id: str, payload: Mapping[str, Any], profile_builder: ProfileBuilder | None
) -> TrackProfile | None:
    """The track's lighting profile from its anlz payload, or None (logged) when it cannot be built."""
    if profile_builder is None:
        return None
    try:
        return profile_builder(payload, {"stable_id": track_id})
    except Exception as exc:  # noqa: BLE001 - a bad profile must not stop the feed
        log.warning("lighting profile for %s unavailable: %s", track_id, exc)
        return None


class DeckFeedBuilder:
    """Turns feed batches into runtime ``DeckFeed`` rows; a deck that unloads gets one row with profile None."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def feeds(self, batch: FeedBatch) -> tuple[DeckFeed, ...]:
        """Runtime deck feeds for one batch, with each loaded deck's profile and cleared rows for unloaded decks."""
        rows: list[DeckFeed] = []
        loaded: set[str] = set()
        for frame in batch.decks:
            loaded.add(frame.sample.deck)
            analysis = frame.analysis
            if analysis is None:
                rows.append(DeckFeed(frame.sample))
                continue
            rows.append(DeckFeed(frame.sample, analysis.grid, analysis.sections, profile=analysis.profile))
        rows.extend(
            DeckFeed(DeckSample(deck, 0.0, 0.0, False, batch.received_ns))
            for deck in sorted(self._seen - loaded, key=int)
        )
        self._seen = loaded
        return tuple(rows)


def track_ids_from_mirror(document: Mapping[str, Any]) -> dict[str, str | None]:
    decks = document.get("decks")
    if not isinstance(decks, Mapping):
        raise EngineFeedError("ui-mirror answer has no 'decks' object")
    return {str(key): (deck or {}).get("stable_id") for key, deck in decks.items()}


def refetch_ids(envelope: Mapping[str, Any]) -> set[str]:
    """Track ids a ``library.changed`` envelope asks to refetch; empty for any other topic or kind."""
    if envelope.get("topic") != "library.changed":
        return set()
    payload = envelope.get("payload") or {}
    if payload.get("kind") not in REFETCH_KINDS:
        return set()
    return {str(i) for i in payload.get("ids") or []}


# ----------------------------------------------------------------------------- I/O


async def discover_engine(http: httpx.AsyncClient) -> EngineEndpoint:
    resp = await http.get(ENGINE_STATUS_PATH)
    if resp.status_code != HTTPStatus.OK:
        raise EngineFeedError(f"GET {ENGINE_STATUS_PATH} answered {resp.status_code}")
    body = resp.json()
    if body.get("state") != "running" or not body.get("ws_url") or not body.get("token"):
        raise EngineFeedUnavailable(f"audio engine is not running (state={body.get('state')!r})")
    return EngineEndpoint(ws_url=body["ws_url"], token=body["token"])


async def _open(url: str, name: str) -> ClientConnection:
    try:
        return await connect(url, open_timeout=WS_OPEN_TIMEOUT_S, compression=None)
    except InvalidStatus as exc:
        status = exc.response.status_code
        if status == HTTPStatus.UNAUTHORIZED:
            raise EngineFeedAuthError(f"{name} refused the token (HTTP 401)") from exc
        raise EngineFeedError(f"{name} handshake answered HTTP {status}") from exc
    except (OSError, TimeoutError) as exc:
        raise EngineFeedError(f"{name} unreachable: {exc}") from exc


class EngineFeed:
    """Async iterator of ``FeedBatch`` from one engine connection; raises on any close."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        endpoint: EngineEndpoint,
        events_url: str,
        mirror_poll_s: float = MIRROR_POLL_S,
        profile_builder: ProfileBuilder | None = None,
    ) -> None:
        self._profile_builder = profile_builder
        self._http = http
        self._endpoint = endpoint
        self._events_url = events_url
        self._mirror_poll_s = mirror_poll_s
        self._track_ids: dict[str, str | None] = {}
        self._mirror_at = float("-inf")
        self._mirror_closed = False
        self._analyses: dict[str, TrackAnalysis] = {}
        self._pending: set[str] = set()
        self._last_shape: tuple[tuple[bool, float], ...] = ()
        self.anlz_fetches: Counter[str] = Counter()

    async def batches(self) -> AsyncIterator[FeedBatch]:
        engine = await _open(f"{self._endpoint.ws_url}?token={quote(self._endpoint.token)}", "audio engine")
        try:
            events = await _open(self._events_url, "events bus")
        except BaseException:
            await engine.close()
            raise
        events_task = asyncio.create_task(self._read_events(events))
        try:
            hello = json.loads(await engine.recv())
            if hello.get("type") != "hello":
                raise EngineFeedError(f"engine's first message is {hello.get('type')!r}, not 'hello'")
            await self._probe_ui_mirror()
            async for raw in engine:
                received_ns = time.monotonic_ns()
                line = json.loads(raw)
                kind = line.get("type")
                if kind in IGNORED_TYPES:
                    continue
                if kind != "state":
                    raise EngineFeedError(f"unknown engine message type {kind!r}")
                if events_task.done():
                    events_task.result()
                    raise EngineFeedDisconnected("events bus closed")
                await self._refresh_track_ids(line)
                await self._attach_analyses(line)
                track_ids = {} if self._mirror_closed else self._track_ids
                analyses = {} if self._mirror_closed else self._analyses
                yield parse_state_line(line, received_ns, track_ids, analyses)
            raise EngineFeedDisconnected(
                f"audio engine closed (code {engine.close_code}, reason {engine.close_reason!r})"
            )
        except ConnectionClosed as exc:
            raise EngineFeedDisconnected(f"audio engine closed: {exc}") from exc
        finally:
            events_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, EngineFeedDisconnected):
                await events_task
            await events.close()
            await engine.close()

    async def _read_events(self, events: ClientConnection) -> None:
        try:
            async for raw in events:
                self._pending |= refetch_ids(json.loads(raw))
        except ConnectionClosed as exc:
            raise EngineFeedDisconnected(f"events bus closed: {exc}") from exc
        raise EngineFeedDisconnected("events bus closed")

    async def _apply_ui_mirror_response(self, resp: httpx.Response) -> None:
        if resp.status_code in (HTTPStatus.CONFLICT, 409):
            if not self._mirror_closed:
                log.warning("ui-mirror: no page open, deck track ids unknown")
            self._mirror_closed = True
            self._track_ids = {}
            self._analyses.clear()
            self._pending.clear()
            return
        if resp.status_code != HTTPStatus.OK:
            raise EngineFeedError(f"GET {UI_MIRROR_PATH} answered {resp.status_code}")
        self._mirror_closed = False
        self._track_ids = track_ids_from_mirror(resp.json())

    async def _probe_ui_mirror(self) -> None:
        resp = await self._http.get(UI_MIRROR_PATH)
        await self._apply_ui_mirror_response(resp)

    async def _refresh_track_ids(self, line: Mapping[str, Any]) -> None:
        if self._mirror_closed:
            self._track_ids = {}
            return
        shape = tuple((bool(d.get("loaded")), float(d.get("duration_ms") or 0.0)) for d in line.get("decks") or [])
        due = time.monotonic() - self._mirror_at >= self._mirror_poll_s
        if not due and shape == self._last_shape:
            return
        self._last_shape = shape
        self._mirror_at = time.monotonic()
        resp = await self._http.get(UI_MIRROR_PATH)
        await self._apply_ui_mirror_response(resp)

    async def _attach_analyses(self, line: Mapping[str, Any]) -> None:
        if self._mirror_closed:
            return
        loaded = {
            tid: float(d["duration_ms"])
            for d in line["decks"]
            if d.get("loaded") and (tid := self._track_ids.get(str(d["deck"])))
        }
        pending, self._pending = self._pending, set()
        for tid, duration_ms in loaded.items():
            if tid not in self._analyses or tid in pending:
                self._analyses[tid] = await self._fetch_analysis(tid, duration_ms)

    async def _fetch_analysis(self, track_id: str, duration_ms: float) -> TrackAnalysis:
        if self._mirror_closed:
            raise EngineFeedError("ui-mirror closed, track analysis unavailable")
        path = ANLZ_PATH.format(stable_id=quote(track_id, safe=""))
        resp = await self._http.get(path, params={"points": ANLZ_POINTS})
        self.anlz_fetches[track_id] += 1
        if resp.status_code != HTTPStatus.OK:
            raise EngineFeedError(f"GET {path} for {track_id} answered {resp.status_code}")
        return analysis_from_anlz(track_id, resp.json(), duration_ms, self._profile_builder)
