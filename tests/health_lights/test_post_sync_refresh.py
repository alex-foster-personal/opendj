"""A CloudSync pull that brought rows in must make the health lights refetch.

Regression lines:
  - if a sync that pulled rows publishes no library.changed then broken
  - if a sync that pulled nothing publishes library.changed then broken

[if] a sync that pulled rows publishes no library.changed [then] fail, [else stop].
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI

from apps.shared import events
from apps.webui.server import cloudsync_scheduler
from apps.webui.server.routes.feedback_sync import FeedbackSyncOut

pytestmark = pytest.mark.requirement("HEALTH-03")


class Hub:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, payload))


class Wakeable:
    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


@pytest.fixture
def hub() -> Iterator[Hub]:
    recorder = Hub()
    events.set_hub(recorder)
    yield recorder
    events.set_hub(None)


def _result(pulled: int) -> FeedbackSyncOut:
    return FeedbackSyncOut(
        status="ok", exported=0, imported=0, pushed=0, pulled=pulled, message="done"
    )


def test_a_pull_with_rows_announces_a_library_change_and_wakes_the_drain(hub: Hub) -> None:
    app = FastAPI()
    app.state.coverage_drain = Wakeable()

    cloudsync_scheduler.announce_pull(app, _result(pulled=3))

    assert hub.published == [("library.changed", {"kind": "tracks", "ids": []})]
    assert app.state.coverage_drain.wakes == 1


def test_a_pull_with_no_rows_announces_nothing(hub: Hub) -> None:
    """Overshoot control: an idle sync round must not make every client refetch."""
    app = FastAPI()
    app.state.coverage_drain = Wakeable()

    cloudsync_scheduler.announce_pull(app, _result(pulled=0))

    assert hub.published == []
    assert app.state.coverage_drain.wakes == 0
