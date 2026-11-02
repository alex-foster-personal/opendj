"""Tests for shared deck-source event emission helper."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from apps.sets.sources.event_emit import emit_recorded_event
from apps.sets.state import Event, SetsState


def _sample_event() -> Event:
    return Event(
        session_id="2026-04-17T21-30-00",
        timestamp_s=1.0,
        wall_clock="2026-04-17T21:30:01.000Z",
        action="track_loaded",
        source="djay_monitor",
    )


def test_emit_recorded_event_logs_record_failure_under_passed_logger(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    source_logger = logging.getLogger("apps.sets.sources.djay_source")
    state = SetsState(db_path=tmp_path / "sets.db")

    with caplog.at_level(logging.WARNING, logger="apps.sets.sources.djay_source"):
        emit_recorded_event(
            state=state,
            event=_sample_event(),
            on_event_cb=None,
            log_label="djay_source",
            logger=source_logger,
        )

    assert len(caplog.records) == 1
    assert caplog.records[0].name == "apps.sets.sources.djay_source"
    assert "djay_source: record_event failed:" in caplog.records[0].message
    assert "FOREIGN KEY constraint failed" in caplog.records[0].message


def test_emit_recorded_event_logs_callback_failure_under_passed_logger(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    source_logger = logging.getLogger("apps.sets.sources.rb_source")
    state = SetsState(db_path=tmp_path / "sets.db")
    state.open_session(
        _sample_event().session_id,
        capture_device="test-device",
        started_at="2026-04-17T21:30:00.000Z",
    )

    def _boom(_event: Event) -> None:
        raise ValueError("callback")

    with caplog.at_level(logging.WARNING, logger="apps.sets.sources.rb_source"):
        emit_recorded_event(
            state=state,
            event=_sample_event(),
            on_event_cb=_boom,
            log_label="rb_source",
            logger=source_logger,
        )

    assert len(caplog.records) == 1
    assert caplog.records[0].name == "apps.sets.sources.rb_source"
    assert "rb_source: on_event callback raised: callback" in caplog.records[0].message
