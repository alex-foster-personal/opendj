"""REVIEW-22: every non-blocking open thread lands in the PR's one follow-up issue.

`render_body` is pure and runs everywhere; the gh half is exercised live by
`just review-triage <pr>`, and is not mocked here (AGENTS.md).
"""

from __future__ import annotations

import pytest

from scripts import review_followups
from scripts.review_thread_parse import PullRequest, build_thread
from tests.scripts.review_thread_helpers import CODEX_BODY, _thread_node

_P0 = "![P0 Badge](https://img.shields.io/badge/P0-red?style=flat) Deletes the library"


def _pr(*nodes: dict) -> PullRequest:
    threads = tuple(t for t in (build_thread(n) for n in nodes) if t is not None)
    return PullRequest(number=7, title="t", state="OPEN", merged=False, url="u", head_sha="c" * 40, threads=threads)


@pytest.mark.requirement("REVIEW-22")
def test_every_follow_up_is_in_the_issue_body_and_no_blocker_is() -> None:
    """[if] a PR has a bot P1 and a P0 [then] only the P1 is filed in the issue body, [else stop]."""
    pr = _pr(_thread_node(body=CODEX_BODY), _thread_node(body=_P0))
    assert len(pr.follow_ups) == 1 and len(pr.failing) == 1
    body = review_followups.render_body(pr.number, pr.follow_ups)
    assert body.startswith(review_followups.marker(7))
    assert "- [ ] [P1/unmarked]" in body
    assert pr.follow_ups[0].permalink in body
    assert "P0" not in body.split("\n", 2)[2]  # the blocker is the gate's, not the issue's


def test_the_body_is_deterministic_for_the_same_threads() -> None:
    pr = _pr(_thread_node(body=CODEX_BODY), _thread_node(body="NON-BLOCKING: nit"))
    first = review_followups.render_body(pr.number, pr.follow_ups)
    assert first == review_followups.render_body(pr.number, tuple(reversed(pr.follow_ups)))


def test_a_ticked_box_survives_a_rewrite_and_is_read_back() -> None:
    pr = _pr(_thread_node(body=CODEX_BODY))
    link = pr.follow_ups[0].permalink
    ticked = review_followups.ticked_links(review_followups.render_body(pr.number, pr.follow_ups, {link: True}))
    assert ticked == {link: True}
    assert review_followups.ticked_links(review_followups.render_body(pr.number, pr.follow_ups)) == {link: False}


def test_a_pr_with_no_follow_ups_files_nothing() -> None:
    """No threads: returns before any gh call, so this needs no network."""
    assert review_followups.file_follow_ups("o", "r", 7, ()) is None
