"""PLAY-18: every deck stop the page reports in its mirror is one engine log line.

Soak round 4 (Tue 6 Oct 2026) found a stop with zero log evidence. The page
records each stop's cause and publishes the latest as ``decks[n].last_stop``;
the engine logs each new one at WARNING, where the engine log keeps it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes import state as state_routes

ARMED = {"autoplay_enabled": True, "autoplay_armed": True, "autoplay_disarm_reason": None}


def _stop(seq: int, cause: str = "user-ui") -> dict[str, Any]:
    return {
        "seq": seq,
        "cause": cause,
        "user_pause": cause == "user-ui",
        "position_ms": 61_000,
        "stable_id": "a" * 40,
        "at": "2026-10-06T05:20:00.000Z",
    }


def _doc(stop: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "client_open": True,
        "client_id": "tab-a",
        "mirror_schema": 2,
        **ARMED,
        "decks": {"1": {"playing": False, "last_stop": stop}, "2": {"playing": True, "last_stop": None}},
    }


def _publish(*documents: dict[str, Any]) -> None:
    app = FastAPI()
    app.include_router(state_routes.router, prefix="/api/v1")

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for document in documents:
                response = await client.put(
                    "/api/v1/state/ui-mirror", json=document, headers={"x-opendj-lease": "tab-a"}
                )
                assert response.status_code == 202

    asyncio.run(run())


def _stop_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("deck-stop ")]


@pytest.mark.requirement("PLAY-18")
def test_a_new_last_stop_is_logged_once_with_its_cause(caplog: pytest.LogCaptureFixture) -> None:
    """[if] a deck's last_stop changes [then] one WARNING names deck and cause, [else stop]."""
    caplog.set_level(logging.WARNING, logger=state_routes.__name__)
    _publish(_doc(None), _doc(_stop(1)), _doc(_stop(1)))
    lines = _stop_lines(caplog)
    assert len(lines) == 1, lines
    assert lines[0].startswith("deck-stop deck=1 cause=user-ui user_pause=True seq=1 missed=0 position_ms=61000")
    assert all(r.levelno == logging.WARNING for r in caplog.records if r.getMessage().startswith("deck-stop "))


@pytest.mark.requirement("PLAY-18")
def test_stops_overwritten_between_publishes_are_counted_as_missed(caplog: pytest.LogCaptureFixture) -> None:
    """[if] seq jumps from 1 to 4 [then] the line says missed=2, [else stop]."""
    caplog.set_level(logging.WARNING, logger=state_routes.__name__)
    _publish(_doc(_stop(1)), _doc(_stop(4, cause="end-of-track")))
    lines = _stop_lines(caplog)
    assert len(lines) == 2, lines
    assert "cause=end-of-track user_pause=False seq=4 missed=2" in lines[1]


@pytest.mark.requirement("PLAY-18")
def test_a_mirror_with_no_stops_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """[if] no deck has a last_stop [then] no deck-stop line is written, [else stop]."""
    caplog.set_level(logging.WARNING, logger=state_routes.__name__)
    _publish(_doc(None), _doc(None))
    assert _stop_lines(caplog) == []
