"""Analysis store fanout bus: lazy shared holder contract (META-01, issue #13068).

Pins the three behaviours of the ``LazyModuleEventBus`` holder in
:mod:`apps.analysis.store`: publishing before anyone subscribes, a subscriber
attached lazily to the shared bus, and an import that starts no bus thread.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.analysis import store as analysis_store
from apps.shared.lazy_event_bus import LazyModuleEventBus
from apps.shared.state.types import Event

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WAIT_S = 5.0


@pytest.fixture
def fresh_bus() -> Iterator[None]:
    """Reset the module holder so the next get builds a real bus; close it after."""
    analysis_store.set_event_bus(None)
    try:
        yield
    finally:
        bus = analysis_store._MODULE_BUS._bus
        if bus is not None:
            bus.close()
        analysis_store.set_event_bus(None)


@pytest.mark.requirement("META-01")
def test_store_uses_shared_lazy_holder() -> None:
    """[if] the store module is loaded [then] it holds a LazyModuleEventBus and no own lock, [else stop]."""
    assert isinstance(analysis_store._MODULE_BUS, LazyModuleEventBus)
    assert not hasattr(analysis_store, "_BUS_LOCK")
    assert not hasattr(analysis_store, "_SHARED_BUS")
    assert "threading" not in vars(analysis_store)


@pytest.mark.requirement("META-01")
@pytest.mark.usefixtures("fresh_bus")
def test_publish_before_first_subscription(tmp_path: Path) -> None:
    """[if] an event is published before any subscriber [then] it persists and builds the bus once, [else stop]."""
    assert analysis_store._MODULE_BUS._bus is None
    db = tmp_path / "state.db"

    first_id = analysis_store.publish_event("beatgrid.flag", {"n": 1}, stable_id="s1", db_path=db)

    assert first_id > 0
    bus = analysis_store._MODULE_BUS._bus
    assert bus is not None
    assert analysis_store._get_shared_bus() is bus
    second_id = analysis_store.publish_event("beatgrid.flag", {"n": 2}, stable_id="s1", db_path=db)
    assert second_id > first_id
    assert analysis_store._get_shared_bus() is bus


@pytest.mark.requirement("META-01")
@pytest.mark.usefixtures("fresh_bus")
def test_lazy_subscriber_receives_later_events(tmp_path: Path) -> None:
    """[if] a subscriber attaches to the lazily built bus [then] it gets later analysis events, [else stop]."""
    received: list[Event] = []
    seen = threading.Event()

    def _on_event(event: Event) -> None:
        """Record the delivered event and wake the test."""
        received.append(event)
        seen.set()

    analysis_store._get_shared_bus().subscribe("beatgrid.flag", _on_event)
    analysis_store.publish_event("beatgrid.flag", {"n": 1}, stable_id="s2", db_path=tmp_path / "state.db")

    assert seen.wait(_WAIT_S)
    assert [e.stable_id for e in received] == ["s2"]
    assert received[0].actor == "apps.analysis"
    assert received[0].payload == {"n": 1}


@pytest.mark.requirement("META-01")
def test_import_starts_no_bus_thread() -> None:
    """[if] apps.analysis.store is imported fresh [then] no EventBus or worker thread exists yet, [else stop]."""
    code = (
        "import json, threading\n"
        "import apps.analysis.store as s\n"
        "print(json.dumps({\n"
        "    'bus_built': s._MODULE_BUS._bus is not None,\n"
        "    'bus_threads': [t.name for t in threading.enumerate()\n"
        "                    if t.name == 'state.events.EventBus'],\n"
        "}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {"bus_built": False, "bus_threads": []}
