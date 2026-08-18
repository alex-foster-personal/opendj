"""Engine event bus seam -- the ONE publish surface for engine events.

Pre-defined contract so parallel builders can wire emit points before the
chassis lands. The chassis registers the live WS hub via set_hub() at
startup. With no hub registered (legacy ``python -m apps.webui.server``
boots, unit tests without the chassis), publish() delivers to nobody by
DESIGN: no WS server means no consumers, so no delivery is the correct
semantics for that process, not a masked failure.
"""

from __future__ import annotations

from typing import Any, Protocol


class EventHub(Protocol):
    def publish(self, topic: str, payload: dict[str, Any]) -> None: ...


_hub: EventHub | None = None


def set_hub(hub: EventHub | None) -> None:
    global _hub
    _hub = hub


def publish(topic: str, payload: dict[str, Any]) -> None:
    if _hub is not None:
        _hub.publish(topic, payload)
