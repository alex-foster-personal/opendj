"""Direct tests for apps.shared.async_scheduler helpers."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from apps.shared.async_scheduler import drain_shielded_in_flight, sleep_until_stop


def test_sleep_until_stop_returns_early_when_event_is_set() -> None:
    async def scenario() -> float:
        stopping = asyncio.Event()
        stopping.set()
        started = time.monotonic()
        await sleep_until_stop(stopping, 1.0)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 0.2


def test_sleep_until_stop_waits_full_interval_when_event_is_unset() -> None:
    async def scenario() -> float:
        stopping = asyncio.Event()
        started = time.monotonic()
        await sleep_until_stop(stopping, 0.05)
        return time.monotonic() - started

    assert asyncio.run(scenario()) >= 0.04


def test_sleep_until_stop_rejects_none() -> None:
    async def scenario() -> None:
        await sleep_until_stop(None, 0.05)

    with pytest.raises(AssertionError):
        asyncio.run(scenario())


def test_drain_shielded_in_flight_completes_when_task_finishes_before_timeout() -> None:
    async def scenario() -> None:
        task = asyncio.create_task(asyncio.to_thread(time.sleep, 0.01))
        await drain_shielded_in_flight(
            task,
            stop_timeout_s=1.0,
            timeout_warning="test round still in flight after %ss",
        )
        assert task.done()

    asyncio.run(scenario())


def test_drain_shielded_in_flight_returns_after_timeout_without_cancelling() -> None:
    async def scenario() -> bool:
        gate = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(gate.wait, 60.0))
        await drain_shielded_in_flight(
            task,
            stop_timeout_s=0.05,
            timeout_warning="test round still in flight after %ss",
        )
        still_running = not task.done()
        gate.set()
        await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        return still_running

    assert asyncio.run(scenario()) is True
