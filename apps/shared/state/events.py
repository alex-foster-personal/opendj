"""In-process event bus + ``FakeEventBus`` test double.

``EventBus`` runs a daemon worker thread that pulls from an internal
``queue.Queue`` and fans events out to subscribers. Subscribers registered
to ``"*"`` receive every event; otherwise the ``kind`` prefix match runs.

Callback exceptions are logged but never propagate, so a noisy subscriber
cannot poison the queue.

``FakeEventBus`` is a synchronous stand-in for tests: it records every
published event and invokes subscribers inline, avoiding thread-timing
flakes.
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence

from .types import Event

log = logging.getLogger(__name__)

Callback = Callable[[Event], None]

_SHUTDOWN = object()


class EventBus:
    """Thread-backed in-process event bus."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._subs: dict[str, list[Callback]] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="state.events.EventBus", daemon=True
        )
        self._thread.start()

    # --- public API -------------------------------------------------

    def publish(self, event: Event) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("EventBus is closed")
            self._q.put(event)

    def subscribe(self, kind: str, callback: Callback) -> None:
        with self._lock:
            self._subs.setdefault(kind, []).append(callback)

    def close(self, timeout: float | None = 5.0) -> None:
        """Drain the queue and stop the worker thread."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._q.put(_SHUTDOWN)
        self._thread.join(timeout=timeout)

    # --- worker -----------------------------------------------------

    def _subscribers_for(self, kind: str) -> Sequence[Callback]:
        with self._lock:
            return list(self._subs.get(kind, [])) + list(self._subs.get("*", []))

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is _SHUTDOWN:
                return
            assert isinstance(item, Event)
            for cb in self._subscribers_for(item.kind):
                try:
                    cb(item)
                except Exception:
                    log.exception("EventBus subscriber raised for kind=%s", item.kind)


class FakeEventBus:
    """Synchronous test double matching EventBus's public API.

    ``events`` collects everything published in order; subscribers are
    invoked inline. Deterministic and cheap in tests.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._subs: dict[str, list[Callback]] = {}

    def publish(self, event: Event) -> None:
        self.events.append(event)
        for cb in list(self._subs.get(event.kind, [])) + list(self._subs.get("*", [])):
            try:
                cb(event)
            except Exception:
                log.exception(
                    "FakeEventBus subscriber raised for kind=%s", event.kind
                )

    def subscribe(self, kind: str, callback: Callback) -> None:
        self._subs.setdefault(kind, []).append(callback)

    def close(self, timeout: float | None = None) -> None:  # noqa: ARG002
        return None


__all__ = ["EventBus", "FakeEventBus", "Callback"]
