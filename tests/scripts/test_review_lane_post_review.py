"""Concurrency regression for post_review head race (issue #272)."""

from __future__ import annotations

import pytest

from scripts.review_gh import TriageError
from scripts.review_lane import _require_post_head_unchanged, post_review

PR = "1234"
OLD_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEW_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_require_post_head_unchanged_refuses_moved_head() -> None:
    """[if] PR head changes after review completes [then] guard fails, [else stop]."""
    with pytest.raises(TriageError, match=r"moved from .* while the review ran"):
        _require_post_head_unchanged(PR, OLD_SHA, NEW_SHA)


def test_post_review_refuses_when_head_moves_before_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] PR head changes after review completes [then] post_review posts nothing, [else stop]."""
    monkeypatch.setattr("scripts.review_lane.pinned_head", lambda _pr: NEW_SHA)
    with pytest.raises(TriageError, match=r"moved from .* while the review ran"):
        post_review(PR, OLD_SHA, "summary", [], "<!-- marker -->", "[test]")


def test_post_review_submits_review_as_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] post_review uploads a review [then] `_gh` uses `as_human=True`, [else stop]."""
    captured: dict[str, object] = {}

    def _fake_gh(*args: object, **kwargs: object) -> str:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "https://github.com/example/pull/1#pullrequestreview-1\n"

    monkeypatch.setattr("scripts.review_lane.pinned_head", lambda _pr: OLD_SHA)
    monkeypatch.setattr("scripts.review_lane._gh", _fake_gh)
    url = post_review(PR, OLD_SHA, "summary", [], "<!-- marker -->", "[test]")
    assert url == "https://github.com/example/pull/1#pullrequestreview-1"
    assert captured["kwargs"] == {"as_human": True}
