"""INFRA-01 event-bus tests.

Uses the real :class:`EventBus` (daemon thread) plus a
``threading.Event``-gated callback so we can assert delivery happened.
"""
from __future__ import annotations

import threading

import pytest

from apps.shared.state.events import EventBus, FakeEventBus
from apps.shared.state.types import Event


pytestmark = pytest.mark.requirement("INFRA-01")


def _make_event(kind: str = "track.upsert") -> Event:
    return Event(ts="2026-02-02T09:00:00Z", kind=kind, stable_id="abc", payload={})


def test_publish_fanout_to_subscribers() -> None:
    bus = EventBus()
    done = threading.Event()
    received: list[Event] = []

    def cb(ev: Event) -> None:
        received.append(ev)
        done.set()

    try:
        bus.subscribe("track.upsert", cb)
        bus.publish(_make_event("track.upsert"))
        assert done.wait(timeout=2.0), "callback not called within 2s"
        assert received and received[0].kind == "track.upsert"
    finally:
        bus.close()


def test_star_subscriber_gets_all_events() -> None:
    bus = EventBus()
    total = 3
    seen = threading.Semaphore(0)
    kinds: list[str] = []

    def cb(ev: Event) -> None:
        kinds.append(ev.kind)
        seen.release()

    try:
        bus.subscribe("*", cb)
        bus.publish(_make_event("track.upsert"))
        bus.publish(_make_event("track.field.set"))
        bus.publish(_make_event("playlist.insert"))
        for _ in range(total):
            assert seen.acquire(timeout=2.0)
        assert set(kinds) == {"track.upsert", "track.field.set", "playlist.insert"}
    finally:
        bus.close()


def test_callback_exception_is_swallowed() -> None:
    bus = EventBus()
    good_done = threading.Event()
    good_received: list[Event] = []

    def bad(ev: Event) -> None:
        raise RuntimeError("boom")

    def good(ev: Event) -> None:
        good_received.append(ev)
        good_done.set()

    try:
        bus.subscribe("track.upsert", bad)
        bus.subscribe("track.upsert", good)
        bus.publish(_make_event())
        assert good_done.wait(timeout=2.0)
        assert len(good_received) == 1
    finally:
        bus.close()


def test_close_is_idempotent() -> None:
    bus = EventBus()
    bus.close()
    bus.close()  # second close must not raise.


def test_publish_after_close_raises() -> None:
    bus = EventBus()
    bus.close()
    with pytest.raises(RuntimeError):
        bus.publish(_make_event())


def test_publish_close_race_does_not_corrupt_state() -> None:
    """Regression: publish and close both guard _closed under the lock.

    If publish reads _closed without the lock (TOCTOU), a concurrent
    close() could set _closed=True between the check and the put(),
    leaving an event in the queue after the SHUTDOWN sentinel.
    """
    bus = EventBus()
    errors: list[BaseException] = []

    def publisher() -> None:
        for _ in range(50):
            try:
                bus.publish(_make_event())
            except RuntimeError:
                pass
            except BaseException as exc:
                errors.append(exc)

    threads = [threading.Thread(target=publisher) for _ in range(4)]
    for t in threads:
        t.start()
    bus.close(timeout=3.0)
    for t in threads:
        t.join(timeout=2.0)
    assert not errors, f"unexpected errors: {errors}"


def test_fake_event_bus_records_inline() -> None:
    bus = FakeEventBus()
    received: list[Event] = []
    bus.subscribe("*", received.append)
    bus.publish(_make_event("track.upsert"))
    assert bus.events[0].kind == "track.upsert"
    assert received[0].kind == "track.upsert"


def test_fake_event_bus_swallows_callback_errors() -> None:
    bus = FakeEventBus()
    bus.subscribe("*", lambda ev: (_ for _ in ()).throw(RuntimeError("x")))
    bus.publish(_make_event())
    assert len(bus.events) == 1
