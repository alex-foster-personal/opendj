"""Regression coverage for the engine's warn-and-above JSONL stream."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.engine_core import log_disk, warning_log
from apps.engine_core.warning_log import _WarningJsonHandler, configure_warning_log

pytestmark = pytest.mark.usefixtures("hermetic_rotation_gate")


@pytest.fixture
def uvicorn_error_logger_at_default_level() -> Iterator[logging.Logger]:
    """The uvicorn.error logger's level belongs to this test, not to the process.

    uvicorn.Config(log_level=...) pins logging.getLogger("uvicorn.error") at that
    level for the rest of the process; 16 tests start an in-process uvicorn, and one
    of them (tests/agentic_testing/test_engine_host.py) runs at "error". Whichever
    of them the CI split seats before this file drops the uvicorn warning below,
    so the stream held 1 record where 2 were written (PR #3396, fast tier leg 1).
    The engine boots uvicorn with log_config=None and its own log level, so the
    production contract is the logger's default level, restored here.
    """
    uvicorn_error = logging.getLogger("uvicorn.error")
    saved_level, saved_disabled = uvicorn_error.level, uvicorn_error.disabled
    uvicorn_error.setLevel(logging.NOTSET)
    uvicorn_error.disabled = False
    try:
        yield uvicorn_error
    finally:
        uvicorn_error.setLevel(saved_level)
        uvicorn_error.disabled = saved_disabled


def test_warning_log_writes_warning_with_boot_id(
    tmp_path: Path, uvicorn_error_logger_at_default_level: logging.Logger
) -> None:
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


def test_warning_log_emit_swallows_enospc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handler = _WarningJsonHandler(tmp_path / "engine-warn.log", "boot-enospc")
    record = logging.LogRecord(
        name="tests.warning_log",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="disk full probe",
        args=(),
        exc_info=None,
    )
    original_open = Path.open

    def _raise_enospc(self: Path, *args: object, **kwargs: object) -> object:
        if self == handler.path:
            raise OSError(28, "No space left on device")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _raise_enospc)
    handler.emit(record)
    captured = capsys.readouterr()
    assert "no space left on device" in captured.err


def test_warning_log_scheduler_survives_enospc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handler = _WarningJsonHandler(tmp_path / "engine-warn.log", "boot-enospc-2")
    record = logging.LogRecord(
        name="tests.warning_log",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="second emit probe",
        args=(),
        exc_info=None,
    )
    original_open = Path.open
    calls = 0

    def _raise_enospc(self: Path, *args: object, **kwargs: object) -> object:
        nonlocal calls
        if self == handler.path:
            calls += 1
            raise OSError(28, "No space left on device")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _raise_enospc)
    handler.emit(record)
    handler.emit(record)
    assert calls == 2
    assert capsys.readouterr().err.count("no space left on device") == 2


def test_warning_log_caps_archive_count_at_twenty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "engine-warn.log"
    for index in range(25):
        archive = tmp_path / f"engine-warn.log.202609{index:02}T000000Z"
        archive.write_text(f"archive-{index}", encoding="utf-8")
        os.utime(archive, (index + 1, index + 1))
    path.write_text("live", encoding="utf-8")
    monkeypatch.setattr(warning_log, "MAX_BYTES", 3)

    warning_log._rotate_if_needed(path, 1)

    archives = list(tmp_path.glob("engine-warn.log.*"))
    assert len(archives) <= warning_log.MAX_ARCHIVE_COUNT


def test_warning_log_skips_rotation_on_low_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "engine-warn.log"
    path.write_text("live", encoding="utf-8")
    monkeypatch.setattr(warning_log, "MAX_BYTES", 3)
    log_disk.reset_rotation_state_for_tests()
    monkeypatch.setattr(
        log_disk,
        "disk_free_bytes",
        lambda _path: 0,
    )

    with caplog.at_level(logging.ERROR, logger="apps.engine_core.log_disk"):
        warning_log._rotate_if_needed(path, 1)
        warning_log._rotate_if_needed(path, 1)

    assert path.exists()
    errors = [rec for rec in caplog.records if rec.levelname == "ERROR"]
    assert len(errors) == 1
    assert "rotation disabled" in errors[0].message


def test_warning_log_rotation_resumes_after_disk_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "engine-warn.log"
    path.write_text("live", encoding="utf-8")
    monkeypatch.setattr(warning_log, "MAX_BYTES", 3)
    log_disk.reset_rotation_state_for_tests()
    monkeypatch.setattr(log_disk, "disk_free_bytes", lambda _path: 0)

    warning_log._rotate_if_needed(path, 1)
    assert path.exists()

    log_disk.reset_rotation_state_for_tests()
    monkeypatch.setattr(
        log_disk,
        "disk_free_bytes",
        lambda _path: log_disk.ENGINE_LOG_MIN_FREE_BYTES,
    )
    warning_log._rotate_if_needed(path, 1)
    assert not path.exists()
    assert len(list(tmp_path.glob("engine-warn.log.*"))) == 1
