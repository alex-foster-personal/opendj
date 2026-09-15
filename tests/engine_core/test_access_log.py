"""Regression coverage for uvicorn access log rate limiting."""

from __future__ import annotations

import logging

import pytest

from apps.engine_core.access_log import (
    ACCESS_LOG_MAX_LINES_PER_SECOND,
    ACCESS_LOG_SUSTAINED_SECONDS,
    AccessLogRateFilter,
    AccessLogRateGate,
    configure_access_log_sampling,
)
from apps.engine_core.warning_log import configure_warning_log


def test_access_log_mutes_sustained_flood_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = AccessLogRateGate(
        max_lines_per_second=50,
        window_seconds=60,
    )
    threshold = ACCESS_LOG_MAX_LINES_PER_SECOND * ACCESS_LOG_SUSTAINED_SECONDS
    with caplog.at_level(logging.WARNING, logger="apps.engine_core.access_log"):
        for _ in range(threshold):
            assert gate.allow(now=30.0)
        assert not gate.allow(now=30.0)
    warnings = [
        rec.message
        for rec in caplog.records
        if rec.levelname == "WARNING" and "muted" in rec.message
    ]
    assert len(warnings) == 1
    assert "lines/s" in warnings[0]


def test_access_log_allows_steady_low_rate() -> None:
    gate = AccessLogRateGate(max_lines_per_second=50, window_seconds=60)
    for second in range(2):
        for offset in range(10):
            assert gate.allow(now=float(second) + offset * 0.05)


def test_configure_access_log_sampling_attaches_to_uvicorn_access(
    tmp_path,
) -> None:
    handler = configure_warning_log(tmp_path / "engine-warn.log", "boot-access")
    access_logger = logging.getLogger("uvicorn.access")
    try:
        configure_access_log_sampling()
        assert any(isinstance(f, AccessLogRateFilter) for f in access_logger.filters)
    finally:
        access_logger.filters = [
            filt
            for filt in access_logger.filters
            if not isinstance(filt, AccessLogRateFilter)
        ]
        logging.getLogger().removeHandler(handler)
