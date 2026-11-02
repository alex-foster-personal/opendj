"""Lazy module-scoped :class:`~apps.shared.state.events.EventBus` holders."""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.shared.state.events import EventBus


class LazyModuleEventBus:
    """Thread-safe lazy holder for one in-process fanout bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bus: EventBus | None = None

    def get(self) -> EventBus:
        with self._lock:
            if self._bus is None:
                from apps.shared.state.events import EventBus

                self._bus = EventBus()
            return self._bus

    def set(self, bus: EventBus | None) -> None:
        with self._lock:
            self._bus = bus
