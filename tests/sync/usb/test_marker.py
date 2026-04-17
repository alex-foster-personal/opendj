"""Tests for apps.sync.usb.marker."""
from __future__ import annotations

import json

import pytest

from apps.sync.usb.marker import MARKER_NAME, read_marker, write_marker


@pytest.mark.requirement("CAT-02")
def test_write_read_round_trip(tmp_path) -> None:
    rev = tmp_path / "r.sh"
    rev.write_text("#!/bin/bash\n", encoding="utf-8")
    path = write_marker(
        tmp_path,
        profile_name="gigA",
        drive_uuid="UUID-1234",
        reversal_log=rev,
        plan_summary={"copy": 3},
    )
    assert path.name == MARKER_NAME
    data = read_marker(tmp_path)
    assert data is not None
    assert data["profile_name"] == "gigA"
    assert data["drive_uuid"] == "UUID-1234"
    assert data["plan_summary"]["copy"] == 3


@pytest.mark.requirement("CAT-02")
def test_read_marker_absent(tmp_path) -> None:
    assert read_marker(tmp_path) is None


@pytest.mark.requirement("CAT-02")
def test_read_marker_corrupt(tmp_path) -> None:
    (tmp_path / MARKER_NAME).write_text("not json")
    assert read_marker(tmp_path) is None
