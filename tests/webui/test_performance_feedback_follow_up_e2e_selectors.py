"""Ratchet for Comment pin follow-up Playwright selectors (issue #11470)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "apps/webui/frontend/tests/e2e/performance-feedback-card-dismiss.spec.ts"


def _spec_source() -> str:
    return SPEC.read_text(encoding="utf-8")


def _follow_up_test_block() -> str:
    text = _spec_source()
    match = re.search(
        r"test\('reopened comment card accepts a follow-up that survives reload'.*?\n\}\);",
        text,
        re.DOTALL,
    )
    assert match, "follow-up acceptance spec test is missing"
    return match.group(0)


@pytest.mark.requirement("FIXT-12834")
def test_follow_up_fill_uses_a_unique_textbox_inside_the_dialog() -> None:
    """[if] follow-up fill matches textarea and button [then] fail, [else stop]."""
    block = _follow_up_test_block()
    assert "followUpFieldInCommentPin" in block
    assert "getByRole('textbox'" in _spec_source()
    assert "FOLLOW_UP_FIELD" in _spec_source()
    assert "page.getByLabel('Follow-up comment')" not in block
    assert re.search(r"getByLabel\('Follow-up comment'\)(?!.*exact)", block) is None


@pytest.mark.requirement("FIXT-12834")
def test_add_follow_up_clicks_the_named_button_inside_the_dialog() -> None:
    """[if] Add follow-up is not the unique dialog button [then] fail, [else stop]."""
    block = _follow_up_test_block()
    assert "addFollowUpButtonInCommentPin" in block
    assert "ADD_FOLLOW_UP_BUTTON" in _spec_source()
    assert "getByRole('button'" in _spec_source()


@pytest.mark.requirement("FIXT-12834")
def test_follow_up_survives_reload_in_fb_note() -> None:
    """[if] follow-up text is missing from .fb-note after reload [then] fail, [else stop]."""
    block = _follow_up_test_block()
    assert "page.reload" in block
    assert ".fb-note').filter({ hasText: followUp })" in block
    assert block.count("await existingPin.click();") >= 2


@pytest.mark.requirement("FIXT-12834")
def test_get_comments_retains_the_follow_up_reply() -> None:
    """[if] GET /comments drops the follow-up reply [then] fail, [else stop]."""
    block = _follow_up_test_block()
    assert "page.request.get('/api/v1/feedback/comments')" in block
    assert "replies?.some((r) => r.text === followUp)" in block
