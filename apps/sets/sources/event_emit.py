"""Shared event recording for deck-state set sources."""
from __future__ import annotations

import logging
from collections.abc import Callable

from ..state import Event, SetsState


def emit_recorded_event(
    *,
    state: SetsState,
    event: Event,
    on_event_cb: Callable[[Event], None] | None,
    log_label: str,
    logger: logging.Logger,
) -> None:
    try:
        state.record_event(event)
    except Exception as exc:  # pragma: no cover -- guard  # noqa: BLE001
        logger.warning("%s: record_event failed: %s", log_label, exc)
    if on_event_cb is not None:
        try:
            on_event_cb(event)
        except Exception as exc:  # pragma: no cover -- guard  # noqa: BLE001
            logger.warning("%s: on_event callback raised: %s", log_label, exc)
