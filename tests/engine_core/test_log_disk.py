"""Regression coverage for log_disk rotation gate helpers."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from apps.engine_core import log_disk


def test_rotation_allowed_logs_once_on_low_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    log_disk.reset_rotation_state_for_tests()
    monkeypatch.setattr(log_disk, "disk_free_bytes", lambda _path: 0)

    with caplog.at_level(logging.ERROR, logger="apps.engine_core.log_disk"):
        assert log_disk.rotation_allowed(tmp_path) is False
        assert log_disk.rotation_allowed(tmp_path) is False

    errors = [rec for rec in caplog.records if rec.levelname == "ERROR"]
    assert len(errors) == 1


def test_rotation_allowed_when_free_space_sufficient(tmp_path: Path) -> None:
    log_disk.reset_rotation_state_for_tests()
    assert log_disk.rotation_allowed(tmp_path) is True
