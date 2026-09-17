"""Credential warning dedup and status notice (CLOUDSYNC-20)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from apps.sync_hub import spoke_credential


@pytest.mark.requirement("CLOUDSYNC-20")
def test_log_hub_verdict_warns_once(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    """[if] the hub reports missing credential twice [then] only one WARNING is logged, [else stop]."""
    caplog.set_level(logging.WARNING)
    spoke_credential.log_hub_verdict("hub-1", "missing")
    spoke_credential.log_hub_verdict("hub-1", "missing")
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_record_credential_notice_persists_for_status(tmp_path: Path) -> None:
    spoke_credential.record_credential_notice(tmp_path, "hub-1", "missing")
    notice = spoke_credential.credential_notice(tmp_path)
    assert notice is not None
    assert notice.verdict == "missing"
    assert notice.hub_machine_id == "hub-1"
    assert "enroll" in notice.action.lower()
