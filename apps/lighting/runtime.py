"""Lighting runtime: show loop frames (LIGHT-62) and daemon feed/events (L06 / #9863).

LightingRuntime.process feeds each engine frame to ShowLoop; sinks are the events hub (show_frame
messages on change) plus any frame_sinks (for example the L24 output loop).
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from apps.lighting.beat_clock import BEATS_PER_BAR, NS_PER_MS, DeckSample, GridBeat, predict_beats
from apps.lighting.clock_source import can_publish_sections, select_frame_clock
from apps.lighting.contract import CONTRACT_VERSION, LIGHTING_FLAG_ID, ClockSnapshot
from apps.lighting.sections import SectionSpan, section_events
from apps.lighting.settings import HANDOVER_KEY, PALETTE_KEY, LightingSettings
from apps.lighting.show_map import Palette, load_palette
from apps.lighting.show_runtime import DeckPlan, DeckState, HandoverMode, ShowFrame, ShowState, step

FrameSink = Callable[[ShowFrame], None]

DEFAULT_LOOKAHEAD_MS = 50.0
FEED_ENDED_CODE = 1000
QUEUE_SIZE = 256
RUNTIME_STOPPED_CODE = 1001
SLOW_CONSUMER_CODE = 1013


class ShowLoop:
    def __init__(self, mode: HandoverMode, sinks: Sequence[FrameSink]) -> None:
        self.mode = mode
        self._sinks: list[FrameSink] = list(sinks)
        self._state = ShowState()

    @property
    def state(self) -> ShowState:
        return self._state

    def add_sink(self, sink: FrameSink) -> None:
        self._sinks.append(sink)

    def tick(self, decks: Sequence[DeckState]) -> ShowFrame:
        self._state, frame = step(self._state, decks, self.mode)
        for sink in self._sinks:
            sink(frame)
        return frame


@dataclass(frozen=True)
class DeckFeed:
    sample: DeckSample
    grid: Sequence[GridBeat] = ()
    sections: Sequence[SectionSpan] = ()
    plan: DeckPlan | None = None
    profile: Any = None


@dataclass(frozen=True)
class FeedFrame:
    host_time_ns: int
    decks: tuple[DeckFeed, ...]
    weights: Mapping[str, float]


class FeedSource(Protocol):
    def frames(self) -> AsyncIterator[FeedFrame]: ...


class ReplayFeed:
    """Plays recorded frames in order; optional ``start_when`` waits before the first frame."""

    def __init__(
        self,
        frames: Sequence[FeedFrame],
        *,
        start_when: Callable[[], Awaitable[None]] | None = None,
        interval_s: float = 0.0,
    ) -> None:
        self._frames = tuple(frames)
        self._start_when = start_when
        self._interval_s = interval_s

    async def frames(self) -> AsyncIterator[FeedFrame]:
        if self._start_when is not None:
            await self._start_when()
        for frame in self._frames:
            if self._interval_s > 0:
                await asyncio.sleep(self._interval_s)
            yield frame


@dataclass(eq=False)
class Subscriber:
    queue: asyncio.Queue[dict[str, Any] | None]
    overflow: asyncio.Event
    close_code: int = FEED_ENDED_CODE
    close_reason: str = ""


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._seq = 0
        self._attach_event = asyncio.Event()

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def attach(self) -> Subscriber:
        sub = Subscriber(queue=asyncio.Queue(maxsize=QUEUE_SIZE), overflow=asyncio.Event())
        self._subscribers.add(sub)
        self._attach_event.set()
        return sub

    def detach(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def publish(self, message: dict[str, Any]) -> None:
        self._seq += 1
        out = {**message, "seq": self._seq}
        dead: list[Subscriber] = []
        for sub in self._subscribers:
            if sub.overflow.is_set():
                continue
            try:
                sub.queue.put_nowait(out)
            except asyncio.QueueFull:
                sub.overflow.set()
                sub.close_code = SLOW_CONSUMER_CODE
                sub.close_reason = "slow consumer"
                dead.append(sub)
        for sub in dead:
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)

    def close_all(self, code: int, reason: str) -> None:
        for sub in self._subscribers:
            sub.close_code = code
            sub.close_reason = reason
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)

    async def wait_for_subscribers(self, count: int) -> None:
        while len(self._subscribers) < count:
            self._attach_event.clear()
            await self._attach_event.wait()


def _bar_beat(n: int | float) -> int:
    return (int(n) - 1) % BEATS_PER_BAR + 1


def _beat_at(grid: Sequence[GridBeat], position_ms: float) -> tuple[int, int] | None:
    if not grid:
        return None
    times = [float(b["time_ms"]) for b in grid]
    i = bisect.bisect_right(times, position_ms) - 1
    if i < 0:
        return 0, _bar_beat(grid[0]["n"])
    if i < len(grid) - 1:
        return i, _bar_beat(grid[i]["n"])
    last_bpm = float(grid[-1]["bpm"])
    if last_bpm <= 0:
        return i, _bar_beat(grid[i]["n"])
    step_ms = 60_000.0 / last_bpm
    offset = int((position_ms - times[-1]) // step_ms)
    last_n = grid[-1]["n"]
    return len(grid) - 1 + offset, (int(last_n) - 1 + offset) % BEATS_PER_BAR + 1


def _section_at(spans: Sequence[SectionSpan], position_ms: float) -> str | None:
    for span in spans:
        if span.start_ms <= position_ms < span.end_ms:
            return span.section.value
    return None


def _phrase_label_at(spans: Sequence[SectionSpan], position_ms: float) -> str | None:
    for span in spans:
        if span.start_ms <= position_ms < span.end_ms:
            return span.raw
    return None


def _bpm_at(grid: Sequence[GridBeat], position_ms: float, rate: float) -> float | None:
    last_bpm: float | None = None
    for beat in grid:
        if float(beat["time_ms"]) <= position_ms:
            last_bpm = float(beat["bpm"]) * rate
        else:
            break
    return last_bpm


def _bar_of(grid: Sequence[GridBeat], beat_index: int) -> int:
    last = len(grid) - 1
    n = int(grid[beat_index]["n"]) if beat_index <= last else int(grid[last]["n"]) + beat_index - last
    return (n - 1) // BEATS_PER_BAR + 1


def _beat_message(event: Any, bar: int) -> dict[str, Any]:
    return {
        "type": "beat",
        "deck": event.deck,
        "beat_index": event.beat_index,
        "bar_beat": event.bar_beat,
        "bar": bar,
        "is_downbeat": event.is_downbeat,
        "bpm": event.bpm,
        "at_host_ns": event.at_host_ns,
        "extrapolated": event.extrapolated,
    }


class LightingRuntime:
    def __init__(
        self,
        feed: FeedSource | None = None,
        *,
        lookahead_ms: float = DEFAULT_LOOKAHEAD_MS,
        show_mode: HandoverMode = HandoverMode.BLEND,
        frame_sinks: Sequence[FrameSink] = (),
        pick_log: Path | None = None,
        message_sinks: Sequence[Callable[[dict[str, Any]], None]] = (),
        clock_provider: Callable[[], ClockSnapshot] = ClockSnapshot,
    ) -> None:
        self.hub = EventHub()
        self.clock_provider = clock_provider
        self._effective_clock_deck: str | None = None
        self._message_sinks = list(message_sinks)
        self._profile_sinks: list[Callable[[str, Any], None]] = []
        self._deck_profile: dict[str, Any] = {}
        self._feed = feed
        self._lookahead_ms = lookahead_ms
        self.feed_connected = False
        self._task: asyncio.Task[None] | None = None
        self._last_position_ms: dict[str, float] = {}
        self._last_beat_index: dict[str, int | None] = {}
        self._last_grid_id: dict[str, int] = {}
        self._deck_state: dict[str, dict[str, Any]] = {}
        self._last_weights: dict[str, float] | None = None
        self._frame_host_ns = 0
        self._last_show_frame: ShowFrame | None = None
        self.show = ShowLoop(show_mode, [self._publish_show_frame, *frame_sinks])
        self.settings = LightingSettings(pick_log)
        self.palette = load_palette(self.settings.get(PALETTE_KEY))
        self._pending_palette: Palette | None = None
        self._pending_handover: HandoverMode | None = None

    def set_palette(self, name: str) -> None:
        """Validate and log a palette pick; frames switch to it on the next downbeat."""
        playing = sum(1 for state in self._deck_state.values() if state.get("playing"))
        self.settings.set(PALETTE_KEY, name, decks_playing=playing)
        pending = load_palette(name)
        self._pending_palette = pending if pending.name != self.palette.name else None

    def set_handover(self, mode: str) -> None:
        """Validate and log a handover mode pick; the show loop switches to it on the next downbeat."""
        playing = sum(1 for state in self._deck_state.values() if state.get("playing"))
        self.settings.set(HANDOVER_KEY, mode, decks_playing=playing)
        pending = HandoverMode(mode)
        self._pending_handover = pending if pending != self.show.mode else None

    @property
    def feed(self) -> FeedSource | None:
        return self._feed

    @feed.setter
    def feed(self, value: FeedSource | None) -> None:
        self._feed = value

    @property
    def has_running_task(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._feed is None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.feed_connected = False
        self.hub.close_all(RUNTIME_STOPPED_CODE, "lighting runtime stopped")

    def current_look(self) -> dict[str, str | None]:
        """Owner-deck scene look and phrase label for GET /lighting/look."""
        scene_id: str | None = None
        phrase_label: str | None = None
        frame = self._last_show_frame
        colour_deck = frame.colour_deck if frame is not None else None
        if frame is not None and not frame.ambient and frame.layers and colour_deck:
            for layer in frame.layers:
                if layer.deck == colour_deck:
                    scene_id = layer.look
                    break
        if colour_deck:
            phrase_label = self._deck_state.get(colour_deck, {}).get("phrase_label")
        return {"scene_id": scene_id, "phrase_label": phrase_label}

    def status(self) -> dict[str, Any]:
        decks = sorted(
            [
                {
                    "deck": deck_id,
                    "bpm": state.get("bpm"),
                    "playing": state.get("playing", False),
                    "section": state.get("section"),
                }
                for deck_id, state in self._deck_state.items()
            ],
            key=lambda row: row["deck"],
        )
        return {
            "flag": LIGHTING_FLAG_ID,
            "enabled": True,
            "contract": CONTRACT_VERSION,
            "feed_connected": self.feed_connected,
            "decks": decks,
            "weights": dict(self._last_weights or {}),
            "subscribers": self.hub.subscriber_count,
            "palette": self.settings.get(PALETTE_KEY),
        }

    def ingest(self, frame: FeedFrame) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        snapshot = self.clock_provider()
        present_decks = {deck_feed.sample.deck for deck_feed in frame.decks}
        effective_deck = select_frame_clock(snapshot, present_decks)
        handover = effective_deck != self._effective_clock_deck
        self._effective_clock_deck = effective_deck
        # An unload or vanished feed entry must not leave a previous track's cursors.
        for deck in set(self._deck_state) - (present_decks & snapshot.loaded_decks):
            self._last_position_ms.pop(deck, None)
            self._last_beat_index.pop(deck, None)
            self._last_grid_id.pop(deck, None)
            self._deck_state.pop(deck, None)
        for deck_feed in frame.decks:
            sample = deck_feed.sample
            deck = sample.deck
            position = sample.position_ms
            selected = deck == effective_deck
            previous = self._last_position_ms.get(deck)

            grid_id = id(deck_feed.grid)
            if self._last_grid_id.get(deck) != grid_id:
                self._last_beat_index[deck] = None
                self._last_grid_id[deck] = grid_id

            if previous is not None and position < previous:
                self._last_beat_index[deck] = None
            elif previous is not None and can_publish_sections(sample, previous, selected=selected, handover=handover):
                for change in section_events(deck_feed.sections, previous, position):
                    if sample.rate <= 0:
                        at_host_ns = sample.host_time_ns
                    else:
                        at_host_ns = sample.host_time_ns + round((change.at_ms - position) / sample.rate * NS_PER_MS)
                    messages.append(
                        {
                            "type": "section_change",
                            "deck": deck,
                            "previous": change.previous.value,
                            "section": change.section.value,
                            "mood": change.mood.value,
                            "at_ms": change.at_ms,
                            "at_host_ns": at_host_ns,
                        }
                    )

            self._last_position_ms[deck] = position

            last_idx = self._last_beat_index.get(deck)
            events = predict_beats(deck_feed.grid, sample, self._lookahead_ms) if selected else ()
            for event in events:
                if last_idx is not None and event.beat_index <= last_idx:
                    continue
                messages.append(_beat_message(event, _bar_of(deck_feed.grid, event.beat_index)))
                last_idx = event.beat_index
            if last_idx is not None:
                self._last_beat_index[deck] = last_idx

            self._deck_state[deck] = {
                "bpm": _bpm_at(deck_feed.grid, position, sample.rate),
                "playing": sample.playing,
                "section": _section_at(deck_feed.sections, position),
                "phrase_label": _phrase_label_at(deck_feed.sections, position),
            }

        weights = dict(frame.weights)
        if weights != (self._last_weights or {}):
            messages.append(
                {
                    "type": "deck_weights",
                    "weights": weights,
                    "at_host_ns": frame.host_time_ns,
                }
            )
            self._last_weights = weights

        return messages

    def show_decks(self, frame: FeedFrame) -> list[DeckState]:
        decks: list[DeckState] = []
        seen: set[str] = set()
        for deck_feed in frame.decks:
            deck = deck_feed.sample.deck
            if deck in seen:
                continue
            seen.add(deck)
            beat = _beat_at(deck_feed.grid, deck_feed.sample.position_ms)
            playing = deck_feed.sample.playing and deck_feed.plan is not None and beat is not None
            beat_index, bar_beat = beat or (0, 1)
            weight = min(1.0, max(0.0, float(frame.weights.get(deck, 0.0))))
            decks.append(
                DeckState(
                    deck=deck,
                    playing=playing,
                    beat_index=beat_index,
                    bar_beat=bar_beat,
                    weight=weight,
                    plan=deck_feed.plan if playing else None,
                )
            )
        return decks

    def _publish_show_frame(self, frame: ShowFrame) -> None:
        if frame == self._last_show_frame:
            return
        self.hub.publish(
            {
                "type": "show_frame",
                "ambient": frame.ambient,
                "colour_deck": frame.colour_deck,
                "position_deck": frame.position_deck,
                "layers": [
                    {
                        "deck": layer.deck,
                        "look": layer.look,
                        "effect_level": layer.effect_level,
                        "intensity": layer.intensity,
                        "hit": layer.hit,
                        "rgb": [list(c) for c in self.palette.colours.get(layer.look, ())],
                    }
                    for layer in frame.layers
                ],
                "palette": self.palette.name,
                "at_host_ns": self._frame_host_ns,
            }
        )
        self._last_show_frame = frame

    def attach_os2l(self, client: Any) -> None:
        """Feed runtime messages and per-deck track profiles to an OS2L client."""
        self._message_sinks.append(client.on_message)
        self._profile_sinks.append(client.set_profile)
        for deck, profile in self._deck_profile.items():
            client.set_profile(deck, profile)

    def _sync_profiles(self, frame: FeedFrame) -> None:
        for deck_feed in frame.decks:
            deck = deck_feed.sample.deck
            if deck in self._deck_profile and self._deck_profile[deck] is deck_feed.profile:
                continue
            self._deck_profile[deck] = deck_feed.profile
            for sink in self._profile_sinks:
                sink(deck, deck_feed.profile)

    def process(self, frame: FeedFrame) -> ShowFrame:
        self._sync_profiles(frame)
        for message in self.ingest(frame):
            self.hub.publish(message)
            if self._pending_palette is not None and message.get("is_downbeat"):
                self.palette, self._pending_palette = self._pending_palette, None
                self._last_show_frame = None
            if self._pending_handover is not None and message.get("is_downbeat"):
                self.show.mode, self._pending_handover = self._pending_handover, None
            for sink in self._message_sinks:
                sink(message)
        self._frame_host_ns = frame.host_time_ns
        return self.show.tick(self.show_decks(frame))

    async def _run(self) -> None:
        feed = self._feed
        if feed is None:
            return
        try:
            self.feed_connected = True
            async for frame in feed.frames():
                self.process(frame)
        finally:
            self.feed_connected = False
            self.hub.close_all(FEED_ENDED_CODE, "lighting feed ended")


__all__ = [
    "DEFAULT_LOOKAHEAD_MS",
    "DeckFeed",
    "EventHub",
    "FeedFrame",
    "FeedSource",
    "FrameSink",
    "LightingRuntime",
    "ReplayFeed",
    "ShowLoop",
    "Subscriber",
]
