"""CLOUDSYNC-09 hydration pool: bounded download parallelism.

[if] memory pressure is elevated [then] the pool admits only 1 concurrent download, [else stop].
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from apps.cloud.hydration_pool import (
    DEFAULT_HYDRATION_MAX_CONCURRENT,
    ENV_HYDRATION_MAX_CONCURRENT,
    effective_limit,
    in_flight,
    reset_for_tests,
    run_download,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-09")


@pytest.fixture(autouse=True)
def _reset_pool() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.mark.requirement("CLOUDSYNC-09")
def test_default_cap_is_four_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the env var is unset [then] the effective limit defaults to 4, [else stop]."""
    monkeypatch.delenv(ENV_HYDRATION_MAX_CONCURRENT, raising=False)
    assert DEFAULT_HYDRATION_MAX_CONCURRENT == 4
    assert effective_limit() == 4


@pytest.mark.requirement("CLOUDSYNC-09")
def test_env_override_raises_on_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the env var is non-integer [then] effective_limit raises ValueError, [else stop]."""
    monkeypatch.setenv(ENV_HYDRATION_MAX_CONCURRENT, "abc")
    with pytest.raises(ValueError, match=ENV_HYDRATION_MAX_CONCURRENT):
        effective_limit()


@pytest.mark.requirement("CLOUDSYNC-09")
def test_fourth_enqueue_blocks_until_slot_frees(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] 3 downloads are in flight [then] a 4th run_download blocks for a slot, [else stop]."""
    monkeypatch.setenv(ENV_HYDRATION_MAX_CONCURRENT, "3")
    entered = threading.Event()
    release = threading.Event()
    fourth_entered = threading.Event()
    max_concurrent = 0
    lock = threading.Lock()

    def slow_work() -> int:
        nonlocal max_concurrent
        with lock:
            max_concurrent = max(max_concurrent, in_flight())
        entered.set()
        release.wait(timeout=5.0)
        return 1

    threads: list[threading.Thread] = []
    for _ in range(3):
        t = threading.Thread(target=lambda: run_download(slow_work))
        t.start()
        threads.append(t)

    for _ in range(50):
        if entered.is_set():
            break
        time.sleep(0.01)
    assert entered.is_set()

    fourth = threading.Thread(
        target=lambda: (run_download(slow_work), fourth_entered.set())
    )
    fourth.start()
    time.sleep(0.1)
    assert not fourth_entered.is_set()
    assert in_flight() == 3

    release.set()
    fourth.join(timeout=5.0)
    for t in threads:
        t.join(timeout=5.0)
    assert fourth_entered.is_set()
    assert max_concurrent == 3


@pytest.mark.requirement("CLOUDSYNC-09")
def test_elevated_pressure_limits_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] pressure is elevated [then] run_download admits only 1 download, [else stop]."""
    monkeypatch.setenv(ENV_HYDRATION_MAX_CONCURRENT, "4")
    pressure: dict[str, Any] = {
        "available": True,
        "kernel_memory_pressure_level": 2,
    }
    entered = threading.Event()
    release = threading.Event()
    max_concurrent = 0
    lock = threading.Lock()

    def slow_work() -> int:
        nonlocal max_concurrent
        with lock:
            max_concurrent = max(max_concurrent, in_flight())
        entered.set()
        release.wait(timeout=5.0)
        return 1

    first = threading.Thread(
        target=lambda: run_download(slow_work, pressure_payload=pressure)
    )
    second = threading.Thread(
        target=lambda: run_download(slow_work, pressure_payload=pressure)
    )
    first.start()
    for _ in range(50):
        if entered.is_set():
            break
        time.sleep(0.01)
    assert entered.is_set()
    second.start()
    time.sleep(0.1)
    assert in_flight() == 1
    assert max_concurrent == 1

    release.set()
    first.join(timeout=5.0)
    second.join(timeout=5.0)


@pytest.mark.requirement("CLOUDSYNC-09")
def test_failure_propagates_and_releases_slot() -> None:
    """[if] the callable raises [then] run_download re-raises and frees the slot, [else stop]."""
    def failing() -> None:
        raise RuntimeError("fetch failed")

    with pytest.raises(RuntimeError, match="fetch failed"):
        run_download(failing)
    assert in_flight() == 0


@pytest.mark.requirement("CLOUDSYNC-09")
def test_pressure_clear_restores_higher_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] pressure clears mid-download [then] a queued download is admitted, [else stop]."""
    monkeypatch.setenv(ENV_HYDRATION_MAX_CONCURRENT, "2")
    pressure_state: dict[str, Any] = {
        "available": True,
        "kernel_memory_pressure_level": 2,
    }
    release_first = threading.Event()
    second_entered = threading.Event()
    max_concurrent = 0
    lock = threading.Lock()

    def slow_work() -> int:
        nonlocal max_concurrent
        with lock:
            max_concurrent = max(max_concurrent, in_flight())
        release_first.wait(timeout=5.0)
        return 1

    first = threading.Thread(
        target=lambda: run_download(slow_work, pressure_payload=pressure_state)
    )
    first.start()
    for _ in range(50):
        if in_flight() == 1:
            break
        time.sleep(0.01)
    assert in_flight() == 1

    pressure_state["kernel_memory_pressure_level"] = 0
    second = threading.Thread(
        target=lambda: (
            run_download(slow_work, pressure_payload=pressure_state),
            second_entered.set(),
        )
    )
    second.start()
    time.sleep(0.1)
    assert not second_entered.is_set()

    release_first.set()
    first.join(timeout=5.0)
    second.join(timeout=5.0)
    assert second_entered.is_set()
    assert max_concurrent >= 1
