"""Regression coverage for the engine's warn-and-above JSONL stream."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.engine_core import warning_log
from apps.engine_core.warning_log import configure_warning_log


def test_warning_log_writes_warning_with_boot_id(tmp_path: Path) -> None:
    handler = configure_warning_log(tmp_path / "engine-warn.log", "boot-test")
    logger = logging.getLogger("tests.engine-warning-log")
    try:
        logger.warning("if the engine warns then the focused JSONL stream exists")
        logging.getLogger("uvicorn.error").warning("if uvicorn warns then it is recorded once")
    finally:
        logging.getLogger().removeHandler(handler)
        uvicorn_error = logging.getLogger("uvicorn.error")
        uvicorn_error.removeHandler(handler)
        uvicorn_error.propagate = True

    records = (tmp_path / "engine-warn.log").read_text(encoding="utf-8").splitlines()
    assert len(records) == 2
    record = json.loads(records[0])
    assert record["boot_id"] == "boot-test"
    assert record["level"] == "WARNING"
    assert record["error_id"].startswith("eid-")
    assert "host" in record
    assert "build_sha" in record
    assert "focused JSONL" in record["message"]
    assert "recorded once" in json.loads(records[1])["message"]


def test_warning_log_rotates_when_the_next_record_would_exceed_cap(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "engine-warn.log"
    path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(warning_log, "MAX_BYTES", 3)

    warning_log._rotate_if_needed(path, 0)

    assert path.read_text(encoding="utf-8") == "old"
    warning_log._rotate_if_needed(path, 1)
    assert not path.exists()
    archives = list(tmp_path.glob("engine-warn.log.*"))
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == "old"


def test_warning_log_prunes_archives_older_than_retention(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "engine-warn.log"
    old_archive = tmp_path / "engine-warn.log.20260101T000000Z"
    recent_archive = tmp_path / "engine-warn.log.20260901T000000Z"
    old_archive.write_text("old", encoding="utf-8")
    recent_archive.write_text("recent", encoding="utf-8")
    eight_days_ago = (datetime.now(UTC) - timedelta(days=8)).timestamp()
    one_day_ago = (datetime.now(UTC) - timedelta(days=1)).timestamp()
    os.utime(old_archive, (eight_days_ago, eight_days_ago))
    os.utime(recent_archive, (one_day_ago, one_day_ago))
    monkeypatch.setattr(warning_log, "MAX_BYTES", 3)
    path.write_text("live", encoding="utf-8")

    warning_log._rotate_if_needed(path, 1)

    assert not old_archive.exists()
    assert recent_archive.exists()
