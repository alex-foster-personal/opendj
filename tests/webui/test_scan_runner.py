"""Hermetic tests for apps.webui.server.scan_runner.ScanRunner."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import pytest

from apps.webui.server.scan_runner import ScanRunner


@dataclass
class TinyResult:
    value: int


def _make_runner(
    *,
    thread_name: str = "test-scan",
    label: str = "test-label",
    on_busy: Literal["refuse", "wait"] = "wait",
    extra_status: Callable[[bool, threading.Event, Any], dict[str, Any]] | None = None,
    on_result: Callable[[TinyResult], None] | None = None,
    shutdown_join_s: float = 5.0,
) -> ScanRunner[TinyResult]:
    return ScanRunner[TinyResult](
        thread_name=thread_name,
        label=label,
        on_busy=on_busy,
        extra_status=extra_status,
        on_result=on_result,
        shutdown_join_s=shutdown_join_s,
    )


def test_start_work_is_single_flight() -> None:
    gate = threading.Event()
    runner = _make_runner()

    def work(stop: threading.Event) -> TinyResult:
        gate.wait(5)
        return TinyResult(1)

    first = runner.start_work(work)
    assert first["state"] == "running"
    second = runner.start_work(lambda s: TinyResult(2))
    assert second["state"] == "running"
    gate.set()
    runner.join(timeout_s=5)


def test_extra_status_keys_on_status() -> None:
    runner = _make_runner(
        extra_status=lambda running, stop, extra: {
            "stopping": running and stop.is_set(),
            "running_scope": extra,
        }
    )
    idle = runner.status()
    assert idle["stopping"] is False
    assert idle["running_scope"] is None

    done = threading.Event()

    def _work_scope_a(stop: threading.Event) -> TinyResult:
        done.wait(5)
        return TinyResult(0)

    runner.start_work(_work_scope_a, extra="scope-a")
    running = runner.status()
    assert running["state"] == "running"
    assert running["running_scope"] == "scope-a"
    runner.stop()
    assert runner.status()["stopping"] is True
    done.set()
    runner.join(timeout_s=5)


def test_run_work_refuse_while_running() -> None:
    gate = threading.Event()
    runner = _make_runner(on_busy="refuse")

    def _work(stop: threading.Event) -> TinyResult:
        gate.wait(5)
        return TinyResult(1)

    runner.start_work(_work)
    refused = runner.run_work(lambda stop: TinyResult(99))
    assert refused["state"] == "running"
    assert refused.get("last_result") is None or refused["last_result"].get("value") != 99
    gate.set()
    runner.join(timeout_s=5)


def test_run_work_wait_until_idle() -> None:
    gate = threading.Event()
    runner = _make_runner(on_busy="wait", thread_name="wait-scan")

    def _work(stop: threading.Event) -> TinyResult:
        gate.wait(5)
        return TinyResult(1)

    runner.start_work(_work)
    results: list[int] = []

    def blocking() -> None:
        st = runner.run_work(lambda stop: TinyResult(2))
        lr = st.get("last_result")
        results.append(lr["value"] if lr else -1)

    t = threading.Thread(target=blocking)
    t.start()
    gate.set()
    runner.join(timeout_s=5)
    t.join(timeout=5)
    assert results == [2]


def test_join_timeout_raises() -> None:
    gate = threading.Event()
    runner = _make_runner(label="timeout-label")

    def _work(stop: threading.Event) -> TinyResult:
        gate.wait(30)
        return TinyResult(0)

    runner.start_work(_work)
    with pytest.raises(TimeoutError, match="timeout-label scan still running"):
        runner.join(timeout_s=0.01)
    gate.set()
    runner.join(timeout_s=5)


def test_stop_then_fresh_event_allows_next_scan() -> None:
    runner = _make_runner(on_busy="refuse")
    first_stop = threading.Event()

    def _first_work(stop: threading.Event) -> TinyResult:
        first_stop.wait(5)
        return TinyResult(1)

    runner.start_work(_first_work)
    runner.stop()
    first_stop.set()
    runner.join(timeout_s=5)

    second_done = threading.Event()

    def _second_work(stop: threading.Event) -> TinyResult:
        second_done.set()
        return TinyResult(2)

    runner.start_work(_second_work)
    second_done.wait(timeout=5)
    runner.join(timeout_s=5)
    assert runner.status()["last_result"]["value"] == 2


def test_shutdown_idle_returns_promptly() -> None:
    runner = _make_runner()
    runner.shutdown()


def test_on_result_after_lock_no_deadlock() -> None:
    seen: list[str] = []

    def on_result(result: TinyResult) -> None:
        st = runner.status()
        seen.append(st["state"])

    runner = _make_runner(on_result=on_result)
    runner.run_work(lambda stop: TinyResult(7))
    assert seen == ["idle"]


def test_raising_work_sets_last_error_no_on_result() -> None:
    called: list[int] = []
    runner = _make_runner(on_result=lambda r: called.append(r.value))

    def bad(_stop: threading.Event) -> TinyResult:
        raise ValueError("boom")

    runner.run_work(bad)
    st = runner.status()
    assert st["last_error"] == "ValueError: boom"
    assert called == []


def test_last_result_is_asdict() -> None:
    runner = _make_runner()
    runner.run_work(lambda stop: TinyResult(42))
    st = runner.status()
    assert st["last_result"] == {"value": 42}
