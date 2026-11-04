"""Shared asyncio helpers for engine background schedulers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine


async def sleep_until_stop(stopping: asyncio.Event, seconds: float) -> None:
    """Sleep up to ``seconds``, or return early when ``stopping`` is set."""
    assert stopping is not None
    try:
        await asyncio.wait_for(stopping.wait(), timeout=seconds)
    except TimeoutError:
        return


def launch_scheduler_loop(
    *,
    existing_task: asyncio.Task[object] | None,
    twice_message: str,
    initial_delay_s: float,
    loop: Callable[[], Coroutine[object, object, None]],
    task_name: str,
    on_death: Callable[[asyncio.Task[object]], None],
) -> tuple[asyncio.Event, asyncio.Task[object], float]:
    """Create the stopping event, background task, and first due monotonic time."""
    if existing_task is not None:
        raise RuntimeError(twice_message)
    stopping = asyncio.Event()
    next_due = time.monotonic() + initial_delay_s
    task = asyncio.create_task(loop(), name=task_name)
    task.add_done_callback(on_death)
    return stopping, task, next_due


__all__ = ["launch_scheduler_loop", "sleep_until_stop"]
