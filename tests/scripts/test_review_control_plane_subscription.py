"""REVIEW-18: Grok and Cursor subscription reviews count for control-plane dual review."""

from __future__ import annotations

import pytest

from scripts import review_control_plane, review_coverage

_HEAD = "a" * 40
_OTHER_HEAD = "b" * 40


def _submitted_review(
    login: str,
    body: str | None,
    *,
    commit_id: str,
    state: str = "COMMENTED",
) -> dict:
    return {"user": {"login": login}, "state": state, "body": body, "commit_id": commit_id}


def _grok_body(sha: str, model: str, skipped: str = "") -> str:
    tail = f" skipped={skipped}" if skipped else ""
    return f"<!-- grok-review v1 sha={sha} model={model}{tail} -->"


def _cursor_body(sha: str, model: str, skipped: str = "") -> str:
    tail = f" skipped={skipped}" if skipped else ""
    return f"<!-- cursor-review v1 sha={sha} model={model}{tail} -->"


def _sol_marker_only(sha: str) -> str:
    return f"<!-- sol-review v1 sha={sha} seat=local model=gpt-6.1-sol -->"


def _multi_marker_body(*markers: str) -> str:
    return "\n".join(markers)


def _reviewed(
    sol: bool = False,
    grok: bool = False,
    cursor: bool = False,
    claude: bool = False,
) -> dict[str, bool]:
    return {
        "Codex": False,
        "Sol": sol,
        "Claude": claude,
        "Grok": grok,
        "Cursor": cursor,
    }


# ----- subscription_reviewed_at_head -----------------------------------------


@pytest.mark.requirement("REVIEW-18")
def test_valid_grok_marker_at_head_is_recognized() -> None:
    """[if] grok marker matches head and model [then] Grok is true, [else stop]."""
    reviews = [_submitted_review("maintainer", _grok_body(_HEAD, "grok-4.6-build"), commit_id=_HEAD)]
    found = review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())
    assert found == {"Grok": True, "Cursor": False}


@pytest.mark.requirement("REVIEW-18")
def test_valid_cursor_marker_at_head_is_recognized() -> None:
    """[if] cursor marker matches head and model [then] Cursor is true, [else stop]."""
    reviews = [_submitted_review("maintainer", _cursor_body(_HEAD, "composer-2.5"), commit_id=_HEAD)]
    found = review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())
    assert found == {"Grok": False, "Cursor": True}


@pytest.mark.requirement("REVIEW-18")
def test_grok_claude_model_does_not_count() -> None:
    """[if] grok marker names claude-haiku model [then] Grok is false, [else stop]."""
    reviews = [
        _submitted_review(
            "maintainer",
            _grok_body(_HEAD, "claude-haiku-4-5-20251001"),
            commit_id=_HEAD,
        )
    ]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_grok_requested_model_does_not_count() -> None:
    """[if] grok model is requested: [then] Grok is false, [else stop]."""
    reviews = [
        _submitted_review(
            "maintainer",
            _grok_body(_HEAD, "requested:grok-4.6"),
            commit_id=_HEAD,
        )
    ]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_grok_unknown_model_does_not_count() -> None:
    """[if] grok model is UNKNOWN [then] Grok is false, [else stop]."""
    reviews = [_submitted_review("maintainer", _grok_body(_HEAD, "UNKNOWN"), commit_id=_HEAD)]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_stale_grok_sha_does_not_count() -> None:
    """[if] grok marker sha differs from head [then] Grok is false, [else stop]."""
    reviews = [
        _submitted_review(
            "maintainer",
            _grok_body(_OTHER_HEAD, "grok-4.6-build"),
            commit_id=_OTHER_HEAD,
        )
    ]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_grok_marker_at_head_but_stale_commit_id_does_not_count() -> None:
    """[if] marker sha is head but commit_id is stale [then] Grok is false, [else stop]."""
    reviews = [
        _submitted_review(
            "maintainer",
            _grok_body(_HEAD, "grok-4.6-build"),
            commit_id=_OTHER_HEAD,
        )
    ]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_untrusted_login_with_grok_marker_does_not_count() -> None:
    """[if] grok marker is from another login [then] Grok is false, [else stop]."""
    reviews = [_submitted_review("someone-else", _grok_body(_HEAD, "grok-4.6-build"), commit_id=_HEAD)]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_trusted_login_without_marker_does_not_count() -> None:
    """[if] maintainer has no marker [then] both lanes false, [else stop]."""
    reviews = [
        _submitted_review(
            "maintainer",
            "looks like a review but no marker",
            commit_id=_HEAD,
        )
    ]
    found = review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())
    assert found == {"Grok": False, "Cursor": False}


@pytest.mark.requirement("REVIEW-18")
def test_grok_marker_does_not_count_as_cursor() -> None:
    """[if] only a grok marker is present [then] Cursor stays false, [else stop]."""
    reviews = [_submitted_review("maintainer", _grok_body(_HEAD, "grok-4.6-build"), commit_id=_HEAD)]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Cursor"] is False


@pytest.mark.requirement("REVIEW-18")
def test_cursor_marker_does_not_count_as_grok() -> None:
    """[if] only a cursor marker is present [then] Grok stays false, [else stop]."""
    reviews = [_submitted_review("maintainer", _cursor_body(_HEAD, "composer-2.5"), commit_id=_HEAD)]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_pending_grok_review_does_not_count() -> None:
    """[if] grok review state is PENDING [then] Grok is false, [else stop]."""
    reviews = [
        _submitted_review(
            "maintainer",
            _grok_body(_HEAD, "grok-4.6-build"),
            commit_id=_HEAD,
            state="PENDING",
        )
    ]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_dismissed_grok_review_does_not_count() -> None:
    """[if] grok review state is DISMISSED [then] Grok is false, [else stop]."""
    reviews = [
        _submitted_review(
            "maintainer",
            _grok_body(_HEAD, "grok-4.6-build"),
            commit_id=_HEAD,
            state="DISMISSED",
        )
    ]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_subscription_review_missing_commit_id_raises_key_error() -> None:
    """[if] review lacks commit_id key [then] KeyError is raised, [else stop]."""
    reviews = [
        {
            "user": {"login": "maintainer"},
            "state": "COMMENTED",
            "body": _grok_body(_HEAD, "grok-4.6-build"),
        }
    ]
    with pytest.raises(KeyError):
        review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())


@pytest.mark.requirement("REVIEW-18")
def test_null_review_body_does_not_match_lane() -> None:
    """[if] review body is null [then] lane is false with no error, [else stop]."""
    reviews = [_submitted_review("maintainer", None, commit_id=_HEAD)]
    found = review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())
    assert found == {"Grok": False, "Cursor": False}


_CP_HITS = ("scripts/review_control_plane.py",)


@pytest.mark.requirement("REVIEW-18")
def test_grok_and_cursor_markers_in_one_body_count_for_neither() -> None:
    """[if] one body has grok and cursor markers [then] both lanes false, [else stop]."""
    body = _multi_marker_body(
        _grok_body(_HEAD, "grok-4.6-build"),
        _cursor_body(_HEAD, "composer-2.5"),
    )
    reviews = [_submitted_review("maintainer", body, commit_id=_HEAD)]
    found = review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())
    assert found == {"Grok": False, "Cursor": False}


@pytest.mark.requirement("REVIEW-18")
def test_sol_and_grok_markers_in_one_body_grok_does_not_count() -> None:
    """[if] one body has sol and grok markers [then] Grok is false, [else stop]."""
    body = _multi_marker_body(_sol_marker_only(_HEAD), _grok_body(_HEAD, "grok-4.6-build"))
    reviews = [_submitted_review("maintainer", body, commit_id=_HEAD)]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_two_grok_markers_with_different_models_are_ambiguous() -> None:
    """[if] one body has two grok markers with different models [then] Grok false, [else stop]."""
    body = _multi_marker_body(
        _grok_body(_HEAD, "grok-4.6-build"),
        _grok_body(_HEAD, "grok-4.7-high-fast"),
    )
    reviews = [_submitted_review("maintainer", body, commit_id=_HEAD)]
    assert review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, ())["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_grok_skipped_non_control_plane_path_still_counts_with_hits() -> None:
    """[if] skipped names only reqs.json on a cp PR [then] Grok true, [else stop]."""
    reviews = [
        _submitted_review(
            "maintainer",
            _grok_body(_HEAD, "grok-4.6-build", skipped="reqs.json"),
            commit_id=_HEAD,
        )
    ]
    found = review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, _CP_HITS)
    assert found["Grok"] is True


@pytest.mark.requirement("REVIEW-18")
def test_grok_skipped_control_plane_hit_does_not_count() -> None:
    """[if] skipped names a cp hit the PR changes [then] Grok false, [else stop]."""
    reviews = [
        _submitted_review(
            "maintainer",
            _grok_body(_HEAD, "grok-4.6-build", skipped="scripts/review_control_plane.py"),
            commit_id=_HEAD,
        )
    ]
    found = review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, _CP_HITS)
    assert found["Grok"] is False


@pytest.mark.requirement("REVIEW-18")
def test_cursor_skipped_control_plane_hit_does_not_count() -> None:
    """[if] cursor skipped names a cp hit [then] Cursor false, [else stop]."""
    reviews = [
        _submitted_review(
            "maintainer",
            _cursor_body(_HEAD, "composer-2.5", skipped="scripts/review_control_plane.py"),
            commit_id=_HEAD,
        )
    ]
    found = review_control_plane.subscription_reviewed_at_head(reviews, _HEAD, _CP_HITS)
    assert found["Cursor"] is False


# ----- dual_review with subscription harnesses --------------------------------


@pytest.mark.requirement("REVIEW-18")
def test_claude_author_sol_and_grok_at_head_passes() -> None:
    """[if] Claude PR has Sol and Grok at head [then] dual review passes, [else stop]."""
    v = review_control_plane.dual_review(
        ("CLAUDE.md",),
        frozenset({"Claude"}),
        "author Claude",
        _reviewed(sol=True, grok=True),
    )
    assert v.ok is True


@pytest.mark.requirement("REVIEW-18")
def test_claude_author_sol_and_cursor_at_head_passes() -> None:
    """[if] Claude PR has Sol and Cursor at head [then] dual review passes, [else stop]."""
    v = review_control_plane.dual_review(
        ("CLAUDE.md",),
        frozenset({"Claude"}),
        "author Claude",
        _reviewed(sol=True, cursor=True),
    )
    assert v.ok is True


@pytest.mark.requirement("REVIEW-18")
def test_claude_author_sol_only_still_fails() -> None:
    """[if] Claude PR has only Sol at head [then] dual review fails, [else stop]."""
    v = review_control_plane.dual_review(
        ("CLAUDE.md",),
        frozenset({"Claude"}),
        "author Claude",
        _reviewed(sol=True),
    )
    assert v.ok is False


@pytest.mark.requirement("REVIEW-18")
def test_grok_author_cannot_count_own_grok_review() -> None:
    """[if] Grok author has own Grok review [then] it fails without a second, [else stop]."""
    only_self = review_control_plane.dual_review(
        ("justfile",),
        frozenset({"Grok"}),
        "author Grok",
        _reviewed(grok=True),
    )
    assert only_self.ok is False
    sol_and_grok = review_control_plane.dual_review(
        ("justfile",),
        frozenset({"Grok"}),
        "author Grok",
        _reviewed(sol=True, grok=True),
    )
    assert sol_and_grok.ok is False
    sol_and_claude = review_control_plane.dual_review(
        ("justfile",),
        frozenset({"Grok"}),
        "author Grok",
        _reviewed(sol=True, claude=True),
    )
    assert sol_and_claude.ok is True


@pytest.mark.requirement("REVIEW-18")
def test_cursor_author_cannot_count_own_cursor_review() -> None:
    """[if] Cursor author has own Cursor review [then] only one harness counts, [else stop]."""
    sol_and_cursor = review_control_plane.dual_review(
        ("justfile",),
        frozenset({"Cursor"}),
        "author Cursor",
        _reviewed(sol=True, cursor=True),
    )
    assert sol_and_cursor.ok is False
    sol_and_claude = review_control_plane.dual_review(
        ("justfile",),
        frozenset({"Cursor"}),
        "author Cursor",
        _reviewed(sol=True, claude=True),
    )
    assert sol_and_claude.ok is True


# ----- enforce end-to-end ----------------------------------------------------


_PR_ENFORCE = "4999"
_CONTROL_PLANE_FILE = ("scripts/review_control_plane.py",)

_CLAUDE_AUTHOR_COMMIT: dict = {
    "sha": "c" * 40,
    "parents": [{"sha": "d" * 40}],
    "commit": {"message": "feat: x\n\nCo-Authored-By: Someone <a@b.c>\n-Claude"},
}

_GROK_AUTHOR_COMMIT: dict = {
    "sha": "e" * 40,
    "parents": [{"sha": "f" * 40}],
    "commit": {"message": "feat: y\n\n-Grok"},
}


def _sol_review_at_head(head: str) -> dict:
    body = f"Sol review of `{head[:10]}`: 0 findings.\n\n<!-- sol-review v1 sha={head} seat=local model=gpt-6.1-sol -->"
    return _submitted_review("maintainer", body, commit_id=head)


def _grok_review_at_head(head: str, *, state: str = "COMMENTED") -> dict:
    body = f"<!-- grok-review v1 sha={head} model=grok-4.6-build -->"
    return _submitted_review("maintainer", body, commit_id=head, state=state)


def _patch_enforce_github_seam(
    monkeypatch: pytest.MonkeyPatch,
    pr: str,
    head: str,
    reviews: list[dict],
    commits: list[dict],
) -> None:
    monkeypatch.setattr(review_coverage, "_head_sha", lambda _pr: head)
    base = f"repos/{review_coverage.REPO}"
    pages = {
        f"{base}/pulls/{pr}/reviews": reviews,
        f"{base}/pulls/{pr}/comments": [],
        f"{base}/issues/{pr}/comments": [],
        f"{base}/pulls/{pr}/commits": commits,
    }

    def captured(path: str) -> list[dict]:
        if path not in pages:
            raise AssertionError(f"unexpected GitHub REST path: {path}")
        return pages[path]

    monkeypatch.setattr(review_coverage, "_paginated_json_list", captured)


@pytest.mark.requirement("REVIEW-18")
def test_enforce_dual_review_counts_grok_with_sol_at_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Sol and Grok at head via enforce [then] rc is 0 and Sol-only is 1, [else stop]."""
    commits = [_CLAUDE_AUTHOR_COMMIT]
    _patch_enforce_github_seam(
        monkeypatch,
        _PR_ENFORCE,
        _HEAD,
        [_sol_review_at_head(_HEAD), _grok_review_at_head(_HEAD)],
        commits,
    )
    assert review_control_plane.enforce(_PR_ENFORCE, _HEAD, list(_CONTROL_PLANE_FILE)) is True
    _patch_enforce_github_seam(
        monkeypatch,
        _PR_ENFORCE,
        _HEAD,
        [_sol_review_at_head(_HEAD)],
        commits,
    )
    assert review_control_plane.enforce(_PR_ENFORCE, _HEAD, list(_CONTROL_PLANE_FILE)) is False


@pytest.mark.requirement("REVIEW-18")
def test_enforce_dismissed_grok_at_head_fails_despite_sol(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] enforce sees Sol plus DISMISSED Grok at head [then] rc is 1, [else stop]."""
    commits = [_CLAUDE_AUTHOR_COMMIT]
    _patch_enforce_github_seam(
        monkeypatch,
        _PR_ENFORCE,
        _HEAD,
        [_sol_review_at_head(_HEAD), _grok_review_at_head(_HEAD, state="DISMISSED")],
        commits,
    )
    assert review_control_plane.enforce(_PR_ENFORCE, _HEAD, list(_CONTROL_PLANE_FILE)) is False


@pytest.mark.requirement("REVIEW-18")
def test_enforce_grok_author_excludes_own_grok_review(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] Grok author has Sol and Grok reviews [then] enforce returns 1, [else stop]."""
    reviews = [_sol_review_at_head(_HEAD), _grok_review_at_head(_HEAD)]
    _patch_enforce_github_seam(monkeypatch, _PR_ENFORCE, _HEAD, reviews, [_GROK_AUTHOR_COMMIT])
    assert review_control_plane.enforce(_PR_ENFORCE, _HEAD, list(_CONTROL_PLANE_FILE)) is False
    failure = capsys.readouterr().out
    assert failure is not None
    assert "excluded as author: Grok" in failure


@pytest.mark.requirement("REVIEW-18")
def test_enforce_dual_marked_review_does_not_count_as_second_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] Sol plus one grok+cursor body at head [then] enforce rc is 1, [else stop]."""
    dual_body = _multi_marker_body(
        _grok_body(_HEAD, "grok-4.6-build"),
        _cursor_body(_HEAD, "composer-2.5"),
    )
    reviews = [
        _sol_review_at_head(_HEAD),
        _submitted_review("maintainer", dual_body, commit_id=_HEAD),
    ]
    _patch_enforce_github_seam(
        monkeypatch,
        _PR_ENFORCE,
        _HEAD,
        reviews,
        [_CLAUDE_AUTHOR_COMMIT],
    )
    assert review_control_plane.enforce(_PR_ENFORCE, _HEAD, list(_CONTROL_PLANE_FILE)) is False


@pytest.mark.requirement("REVIEW-18")
def test_enforce_grok_skipped_cp_hit_fails_despite_sol(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Sol and Grok skipped the cp file changed [then] enforce rc is 1, [else stop]."""
    body = _grok_body(_HEAD, "grok-4.6-build", skipped="scripts/review_control_plane.py")
    reviews = [
        _sol_review_at_head(_HEAD),
        _submitted_review("maintainer", body, commit_id=_HEAD),
    ]
    _patch_enforce_github_seam(
        monkeypatch,
        _PR_ENFORCE,
        _HEAD,
        reviews,
        [_CLAUDE_AUTHOR_COMMIT],
    )
    assert review_control_plane.enforce(_PR_ENFORCE, _HEAD, list(_CONTROL_PLANE_FILE)) is False
