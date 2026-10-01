"""Enrich-on-open: one card, its every state, and the HTTP face of each answer.

Regression lines:
  - if a fully enriched library still shows the card then broken
  - if missing, failed or unavailable analysis does not show the card then broken
  - if stems are pending with no decision and are not asked then broken
  - if a stored "never" is asked again then broken
  - if stems with no source on this host are asked then broken
  - if the decision is written anywhere but the app's data dir then broken
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apps.webui.server import enrich_prompt as ep
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library

pytestmark = pytest.mark.requirement("ENRICH-01")


def _analysis(**lane_over: dict[str, Any]) -> dict[str, Any]:
    full = {"total": 2, "done": 2, "missing": 0, "failed": 0, "declined": 0, "unavailable": None}
    return {"lanes": {lane: {**full, **lane_over.get(lane, {})} for lane in ep.ANALYSIS_LANES}}


def _coverage(stems_pending: int = 0, lyrics_pending: int = 0, refusal: str | None = None) -> dict[str, Any]:
    return {
        "on_disk": 2,
        "done": {"stems": 2 - stems_pending, "lyrics": 2 - lyrics_pending},
        "terminal": {"stems": 0, "lyrics": 0},
        "failed": {"stems": 0, "lyrics": 0},
        "pending": {"stems": stems_pending, "lyrics": lyrics_pending},
        "stems_source_refusal": refusal,
    }


def _summary(analysis: Any = None, coverage: Any = None, decisions: Any = None) -> dict[str, Any]:
    return ep.build_summary(
        analysis=analysis if analysis is not None else _analysis(),
        analysis_error=None,
        coverage=coverage if coverage is not None else _coverage(),
        coverage_error=None,
        decisions=decisions or {},
    )


def test_a_fully_enriched_library_shows_nothing() -> None:
    assert _summary()["show"] is False


@pytest.mark.parametrize(
    "lane_state",
    [{"missing": 1}, {"failed": 1}, {"unavailable": "ffmpeg lacks soxr"}],
    ids=["missing", "failed", "unavailable"],
)
def test_unfinished_analysis_shows_the_card(lane_state: dict[str, Any]) -> None:
    assert _summary(analysis=_analysis(key=lane_state))["show"] is True


def test_declined_analysis_alone_does_not_nag() -> None:
    """A low-confidence key is an answer, not unfinished work."""
    assert _summary(analysis=_analysis(key={"done": 1, "declined": 1}))["show"] is False


def test_pending_lyrics_show_the_card() -> None:
    assert _summary(coverage=_coverage(lyrics_pending=1))["show"] is True


def test_pending_stems_with_no_decision_are_asked() -> None:
    out = _summary(coverage=_coverage(stems_pending=2))
    assert out["show"] is True and out["stems"] == {"state": "ask", "pending": 2, "reason": None}


def test_a_never_is_not_asked_again() -> None:
    out = _summary(coverage=_coverage(stems_pending=2), decisions={"stems": "never"})
    assert out["stems"]["state"] == "user_declined"
    assert out["show"] is False, "if a stored never still raises the card then broken"


def test_stems_with_no_source_are_not_asked() -> None:
    out = _summary(coverage=_coverage(stems_pending=2, refusal="no stems farm configured"))
    assert out["stems"] == {"state": "no_source", "pending": 2, "reason": "no stems farm configured"}
    assert out["show"] is False


def test_an_unreadable_measurement_shows_the_card_with_its_error() -> None:
    out = ep.build_summary(
        analysis=None, analysis_error="drain not running", coverage=None,
        coverage_error="OSError: boom", decisions={},
    )
    assert out["show"] is True and out["stems"]["state"] == "unknown"


def test_decisions_round_trip_and_refuse_unknowns(tmp_path: Path) -> None:
    assert ep.load_decisions(tmp_path) == {}
    assert ep.save_decision(tmp_path, "stems", "never") == {"stems": "never"}
    assert ep.load_decisions(tmp_path) == {"stems": "never"}
    with pytest.raises(ValueError):
        ep.save_decision(tmp_path, "lyrics", "never")
    with pytest.raises(ValueError):
        ep.save_decision(tmp_path, "stems", "maybe")


def test_http_summary_and_decision(library: Library) -> None:
    audio = fx.audio_file(library.music, "a.mp3")
    fx.seed_track(library.state_db, "a", str(audio))
    summary = library.client.get("/api/v1/enrich/summary")
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["analysis_error"] is not None, "if an unarmed drain reads as complete then broken"
    assert body["stems"]["state"] in {"ask", "no_source"}

    put = library.client.put("/api/v1/enrich/decisions/stems", json={"answer": "never"})
    assert put.status_code == 200, put.text
    assert ep.store_path(library.data_dir).is_file(), "if the decision lands outside the data dir then broken"
    assert library.client.put("/api/v1/enrich/decisions/stems", json={"answer": "maybe"}).status_code == 422
