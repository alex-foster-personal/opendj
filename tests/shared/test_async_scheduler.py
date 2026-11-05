"""Direct tests for apps.shared.async_scheduler.sleep_until_stop."""

from __future__ import annotations

import asyncio
import time

import pytest

from apps.shared.async_scheduler import sleep_until_stop


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
