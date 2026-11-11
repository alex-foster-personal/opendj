"""REVIEW-18: ambiguous lane markers and signature robustness."""

from __future__ import annotations

import importlib
import re

import pytest

from scripts import review_claude, review_control_plane, review_sol
from tests.scripts.test_review_control_plane_subscription import (
    _CLAUDE_AUTHOR_COMMIT,
    _CONTROL_PLANE_FILE,
    _HEAD,
    _PR_ENFORCE,
    _cursor_body,
    _grok_body,
    _patch_enforce_github_seam,
    _sol_marker_only,
    _sol_review_at_head,
    _submitted_review,
)


def _cursor_review_at_head(head: str) -> dict:
    return _submitted_review("maintainer", _cursor_body(head, "composer-2.5"), commit_id=head)


def _ambiguous_sol_grok_body(head: str) -> str:
    return (
        f"Sol review of `{head[:10]}`: 0 findings.\n\n"
        f"<!-- sol-review v1 sha={head} seat=local model=gpt-6.1-sol -->\n"
        f"{_grok_body(head, 'grok-4.6-build')}"
    )


def _claude_marker_only(sha: str, model: str) -> str:
    return f"<!-- claude-review v1 sha={sha} model={model} -->"


# ----- enforce: ambiguous body dropped from all evidence ---------------------


@pytest.mark.requirement("REVIEW-18")
def test_enforce_sol_grok_ambiguous_body_plus_cursor_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] sol+grok one body and Cursor alone at head [then] enforce is 1, [else stop]."""
    reviews = [
        _submitted_review("maintainer", _ambiguous_sol_grok_body(_HEAD), commit_id=_HEAD),
        _cursor_review_at_head(_HEAD),
    ]
    _patch_enforce_github_seam(
        monkeypatch,
        _PR_ENFORCE,
        _HEAD,
        reviews,
        [_CLAUDE_AUTHOR_COMMIT],
    )
    assert review_control_plane.enforce(_PR_ENFORCE, _HEAD, list(_CONTROL_PLANE_FILE)) is False
    failure = capsys.readouterr().out
    assert failure is not None
    assert "Cursor" in failure


@pytest.mark.requirement("REVIEW-18")
def test_enforce_clean_sol_and_cursor_at_head_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] clean Sol and Cursor reviews at head [then] enforce is 0, [else stop]."""
    reviews = [_sol_review_at_head(_HEAD), _cursor_review_at_head(_HEAD)]
    _patch_enforce_github_seam(
        monkeypatch,
        _PR_ENFORCE,
        _HEAD,
        reviews,
        [_CLAUDE_AUTHOR_COMMIT],
    )
    assert review_control_plane.enforce(_PR_ENFORCE, _HEAD, list(_CONTROL_PLANE_FILE)) is True


@pytest.mark.requirement("REVIEW-18")
def test_enforce_claude_grok_double_marked_body_only_sol_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] claude+grok one body and Sol at head [then] enforce is 1, [else stop]."""
    dual_body = f"{_claude_marker_only(_HEAD, 'claude-opus-5-5')}\n{_grok_body(_HEAD, 'grok-4.6-build')}"
    reviews = [
        _submitted_review("maintainer", dual_body, commit_id=_HEAD),
        _sol_review_at_head(_HEAD),
    ]
    _patch_enforce_github_seam(
        monkeypatch,
        _PR_ENFORCE,
        _HEAD,
        reviews,
        [_CLAUDE_AUTHOR_COMMIT],
    )
    assert review_control_plane.enforce(_PR_ENFORCE, _HEAD, list(_CONTROL_PLANE_FILE)) is False
    failure = capsys.readouterr().out
    assert failure is not None
    assert "at head: Sol (REVIEW-13 asked for 2" in failure


# ----- ambiguity detection and signature robustness ----------------------------


@pytest.mark.requirement("REVIEW-18")
def test_two_identical_sol_markers_not_ambiguous() -> None:
    """[if] one body has two identical sol markers [then] not ambiguous, [else stop]."""
    marker = _sol_marker_only(_HEAD)
    body = f"{marker}\n{marker}"
    assert review_control_plane._subscription_review_body_ambiguous(body) is False


@pytest.mark.requirement("REVIEW-18")
def test_two_differing_sol_markers_are_ambiguous() -> None:
    """[if] one body has two sol markers with different models [then] ambiguous, [else stop]."""
    body = f"{_sol_marker_only(_HEAD)}\n<!-- sol-review v1 sha={_HEAD} seat=local model=gpt-6.2-sol -->"
    assert review_control_plane._subscription_review_body_ambiguous(body) is True


@pytest.mark.requirement("REVIEW-18")
def test_two_differing_sol_seats_are_ambiguous() -> None:
    """[if] one body has two sol markers differing only in seat [then] ambiguous, [else stop]."""
    body = f"{_sol_marker_only(_HEAD)}\n<!-- sol-review v1 sha={_HEAD} seat=remote model=gpt-6.1-sol -->"
    assert review_control_plane._subscription_review_body_ambiguous(body) is True


@pytest.mark.requirement("REVIEW-18")
def test_sol_marker_import_guard_raises_on_shape_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] SOL_MARKER group count drifts on reload [then] RuntimeError, [else stop]."""
    wrong = re.compile(r"<!--\s*sol-review\s+v1\s+sha=(\S+)\s*-->", re.IGNORECASE)
    monkeypatch.setattr(review_sol, "SOL_MARKER", wrong)
    try:
        with pytest.raises(RuntimeError, match="SOL_MARKER shape changed"):
            importlib.reload(review_control_plane)
    finally:
        monkeypatch.undo()
        importlib.reload(review_control_plane)


@pytest.mark.requirement("REVIEW-18")
def test_claude_lane_marker_is_imported_regex() -> None:
    """[if] Claude ambiguity marker [then] it is review_claude.CLAUDE_MARKER, [else stop]."""
    assert review_control_plane._AMBIGUITY_LANE_MARKERS["Claude"] is review_claude.CLAUDE_MARKER


@pytest.mark.requirement("REVIEW-18")
def test_two_identical_claude_markers_not_ambiguous() -> None:
    """[if] one body has two identical claude markers [then] not ambiguous, [else stop]."""
    marker = _claude_marker_only(_HEAD, "claude-opus-5-5")
    body = f"{marker}\n{marker}"
    assert review_control_plane._subscription_review_body_ambiguous(body) is False


@pytest.mark.requirement("REVIEW-18")
def test_two_differing_claude_models_are_ambiguous() -> None:
    """[if] two claude markers differ in model [then] ambiguous, [else stop]."""
    body = f"{_claude_marker_only(_HEAD, 'claude-opus-5-5')}\n{_claude_marker_only(_HEAD, 'claude-sonnet-5-5')}"
    assert review_control_plane._subscription_review_body_ambiguous(body) is True


# ----- skipped= path normalization for subscription lanes --------------------


@pytest.mark.parametrize(
    "skipped",
    ["scripts/", "scripts", "./scripts/review_control_plane.py"],
)
@pytest.mark.requirement("REVIEW-18")
def test_grok_skipped_normalized_prefix_does_not_count(skipped: str) -> None:
    """[if] grok skipped lists a cp path prefix [then] Grok is false, [else stop]."""
    hits = review_control_plane.control_plane_hits(["scripts/review_control_plane.py"])
    reviews = [
        _submitted_review(
            "maintainer",
            _grok_body(_HEAD, "grok-4.6-build", skipped=skipped),
            commit_id=_HEAD,
        )
    ]
    found = review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, hits)
    assert found["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_grok_skipped_non_intersecting_still_counts() -> None:
    """[if] grok skipped is reqs.json only [then] Grok is true, [else stop]."""
    hits = review_control_plane.control_plane_hits(["scripts/review_control_plane.py"])
    reviews = [
        _submitted_review(
            "maintainer",
            _grok_body(_HEAD, "grok-4.6-build", skipped="reqs.json"),
            commit_id=_HEAD,
        )
    ]
    found = review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, hits)
    assert found["Grok"] is True
