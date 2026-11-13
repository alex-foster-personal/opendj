"""OS2L client (LIGHT-13806): one ``beat`` per beat of the followed deck to a lighting app over TCP.

Connects to an explicit ``host:port``. The followed deck is the one with the highest weight;
``change`` is true on the first beat after it changes, ``pos`` is a running beat count and ``bpm``
the followed deck BPM. A user offset (-50..+50 ms, default 0) shifts send time. On disconnect the
client reconnects with backoff 0.5, 1, 2, 4, 8 s (capped) and logs each attempt.

With a track profile for the followed deck (LIGHT-13817), beats carry ``strength`` =
``round(100 * bar_energy[bar - 1])`` for the 1-based ``bar`` in the runtime message, and each
section change sends ``btn <old> off`` then ``btn <new> on`` before that beat. Button names come
from ``os2l_buttons.json`` so a user can rename them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from apps.lighting.os2l_codec import Beat, Btn, OutMessage, encode

log = logging.getLogger(__name__)

BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)
OFFSET_MIN_MS = -50.0
OFFSET_MAX_MS = 50.0
BUTTONS_PATH = Path(__file__).with_name("os2l_buttons.json")


def load_buttons(path: Path = BUTTONS_PATH) -> dict[str, str]:
    """The section-label to OS2L button-name table from JSON data."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items()}


def _strength(profile: Any, bar: int | None) -> int | None:
    energy = getattr(profile, "bar_energy", None) if profile is not None else None
    if not energy or bar is None or not 1 <= bar <= len(energy) or energy[bar - 1] is None:
        return None
    return max(0, min(100, round(100 * float(energy[bar - 1]))))


def _section_label(profile: Any, bar: int | None) -> str | None:
    if profile is None or bar is None:
        return None
    for section in getattr(profile, "sections", ()) or ():
        if section.start_bar <= bar < section.start_bar + max(1, section.bars):
            return str(section.label)
    return None


class Os2lClient:
    """Schedules OS2L beats from runtime messages and writes them to one lighting server."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        offset_ms: float = 0.0,
        clock: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        backoff_s: Sequence[float] = BACKOFF_S,
        buttons: Mapping[str, str] | None = None,
    ) -> None:
        if not OFFSET_MIN_MS <= offset_ms <= OFFSET_MAX_MS:
            raise ValueError(f"os2l offset must be in -50..50 ms, got {offset_ms}")
        self.host = host
        self.port = port
        self.offset_ms = float(offset_ms)
        self._clock = clock
        self._sleep = sleep
        self._backoff = tuple(backoff_s)
        self._queue: asyncio.Queue[tuple[int, OutMessage]] = asyncio.Queue()
        self._buttons = dict(load_buttons() if buttons is None else buttons)
        self._profiles: dict[str, Any] = {}
        self._section: str | None = None
        self._weights: dict[str, float] = {}
        self._followed: str | None = None
        self._pos = 0
        self.sent: list[tuple[int, OutMessage]] = []
        self.connections = 0

    def followed_deck(self) -> str | None:
        """The deck with the highest weight, or None before any weights arrive."""
        if not self._weights:
            return None
        return max(sorted(self._weights), key=lambda deck: self._weights[deck])

    def set_profile(self, deck: str, profile: Any) -> None:
        """Attach (or with None, clear) the track profile used for strength and sections on a deck."""
        if profile is None:
            self._profiles.pop(str(deck), None)
        else:
            self._profiles[str(deck)] = profile

    def on_message(self, message: Mapping[str, Any]) -> None:
        """Runtime message sink: tracks deck weights and queues beats of the followed deck."""
        kind = message.get("type")
        if kind == "deck_weights":
            self._weights = {str(k): float(v) for k, v in message["weights"].items()}
            return
        if kind != "beat" or message.get("bpm") is None:
            return
        deck = str(message["deck"])
        followed = self.followed_deck() or deck
        if deck != followed:
            return
        change = self._followed is not None and self._followed != deck
        if change:
            self._pos = 0
        self._followed = deck
        self._pos += 1
        at_ns = int(message["at_host_ns"]) + round(self.offset_ms * 1_000_000)
        profile = self._profiles.get(deck)
        raw_bar = message.get("bar")
        bar = int(raw_bar) if isinstance(raw_bar, int | float) else None
        label = _section_label(profile, bar)
        if label is not None and label != self._section:
            if self._section is not None:
                self._queue.put_nowait((at_ns, Btn(name=self._button(self._section), state="off")))
            self._queue.put_nowait((at_ns, Btn(name=self._button(label), state="on")))
            self._section = label
        beat = Beat(pos=self._pos, bpm=float(message["bpm"]), change=change, strength=_strength(profile, bar))
        self._queue.put_nowait((at_ns, beat))

    def _button(self, label: str) -> str:
        return self._buttons.get(label, label)

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        attempt = 0
        while True:
            log.info("os2l connect attempt %d to %s:%d", attempt + 1, self.host, self.port)
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
            except OSError as exc:
                delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                log.warning("os2l connect failed (%s); retry in %.1f s", exc, delay)
                await self._sleep(delay)
                attempt += 1
                continue
            self.connections += 1
            return reader, writer

    async def run(self) -> None:
        """Connect, send queued beats at their offset time, reconnect when the server goes away."""
        pending: tuple[int, OutMessage] | None = None
        while True:
            reader, writer = await self._connect()
            closed = asyncio.Event()
            drain = asyncio.create_task(self._drain_inbound(reader, closed))
            try:
                while True:
                    if pending is None:
                        pending = await self._next_or_closed(closed)
                        if pending is None:
                            break
                    at_ns, beat = pending
                    wait_s = (at_ns - self._clock()) / 1e9
                    if wait_s > 0:
                        await self._sleep(wait_s)
                    if closed.is_set():
                        break
                    writer.write(encode(beat))
                    await writer.drain()
                    self.sent.append((self._clock(), beat))
                    pending = None
            except (ConnectionError, OSError) as exc:
                log.warning("os2l connection lost: %s", exc)
            finally:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await drain
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            log.warning("os2l server closed the connection; reconnecting")
            await self._sleep(self._backoff[0])

    async def _next_or_closed(self, closed: asyncio.Event) -> tuple[int, OutMessage] | None:
        get = asyncio.ensure_future(self._queue.get())
        shut = asyncio.ensure_future(closed.wait())
        await asyncio.wait({get, shut}, return_when=asyncio.FIRST_COMPLETED)
        shut.cancel()
        if get.done():
            return get.result()
        get.cancel()
        return None

    async def _drain_inbound(self, reader: asyncio.StreamReader, closed: asyncio.Event) -> None:
        try:
            while await reader.read(4096):
                pass
        finally:
            closed.set()


__all__ = ["BACKOFF_S", "BUTTONS_PATH", "Os2lClient", "load_buttons"]
