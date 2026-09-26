"""PERF-CAPTURE-03: unattended S13 capture source contract.

The live Google OAuth hop is a headed, pre-consented Playwright run. These
checks pin the three unattended claims that do not need that hop: replayed
storageState, span collection armed before the click, and an observed login
POST with missing marks classifying as missing-telemetry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SPEC: Path = REPO_ROOT / "apps/webui/frontend/tests/e2e/kpi-login-capture.spec.ts"
CONFIG: Path = REPO_ROOT / "apps/webui/frontend/tests/e2e/playwright.kpi-capture.config.ts"


def _spec() -> str:
    return SPEC.read_text(encoding="utf-8")


def _config() -> str:
    return CONFIG.read_text(encoding="utf-8")


# REQ: PERF-CAPTURE-03
@pytest.mark.requirement("PERF-CAPTURE-03")
def test_capture_replays_a_preconsented_storage_state() -> None:
    """[if] KPI_CAPTURE_GOOGLE_STORAGE_STATE is set [then] Playwright replays it, [else stop]."""
    text = _config()
    assert "KPI_CAPTURE_GOOGLE_STORAGE_STATE" in text
    assert "storageState" in text
    assign_at = text.index("const storageStatePath = process.env.KPI_CAPTURE_GOOGLE_STORAGE_STATE")
    use_at = text.index("storageState")
    assert assign_at < use_at


# REQ: PERF-CAPTURE-03
@pytest.mark.requirement("PERF-CAPTURE-03")
def test_span_collector_is_armed_before_the_sign_in_click() -> None:
    """[if] S13 capture drives Sign in [then] span observation is armed first, [else stop]."""
    text = _spec()
    arm_at = text.index("const collector = armLoginSpanCollector(page);")
    click_at = text.index("await driveSignInClick(page, budget, collector);")
    assert arm_at < click_at
    assert "Arm span collection BEFORE the click" in text


# REQ: PERF-CAPTURE-03
@pytest.mark.requirement("PERF-CAPTURE-03")
def test_observed_login_post_with_missing_marks_is_missing_telemetry() -> None:
    """[if] login POST was observed [then] missing marks are missing-telemetry, [else stop]."""
    text = _spec()
    start = text.index("NOT restored-session")
    end = text.index("login marks present but no perf-span POST")
    block = text[start:end]
    assert "missing-telemetry: the login was submitted but the" in block
    assert "reason: `restored-session" not in block
    assert "reason: 'restored-session" not in block
