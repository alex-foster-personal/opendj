"""The merge lane's pin marker (#858): which pins a merged PR turns green.

No polling, and no live engine: the decision is a pure function of the PR body
and the pin list, so it is tested here without touching a running daemon.

[if] a PR body carries `pin <id> -> <sha>` lines [then] those pins are selected
    and carry their SHA
[if] a PR body says `Fixes #n` [then] every pin whose issue_url is that issue is
    selected
[if] a pin is already merged or archived [then] it is skipped, so a re-run is a
    no-op rather than a second note
[if] a note is appended twice [then] the second run leaves the note unchanged
"""

from __future__ import annotations

import pytest

from scripts.pin_mark_merged import Target, main, merged_note, parse_pr_body, select_merged_pins

THIS_REPO = "private_owner/music-dj-tools"
ISSUE = f"https://github.com/{THIS_REPO}/issues"


def pin(**over) -> dict:
    return {
        "id": "b44c957f082f",
        "text": "jump to master button misaligned",
        "status": "fixed",
        "issue_url": None,
        "agent_note": None,
        **over,
    }


# ----- parse_pr_body ------------------------------------------------------
def test_pin_arrow_sha_lines_are_read_with_their_shas() -> None:
    refs = parse_pr_body(
        "Landed the pin review pass.\n\n"
        "pin b44c957f082f -> b63f6d7b\n"
        "pin 8f60606750c6 -> ce81d24b\n"
    )
    assert refs.pin_shas == {"b44c957f082f": "b63f6d7b", "8f60606750c6": "ce81d24b"}


def test_closing_keywords_are_read_as_issue_numbers() -> None:
    refs = parse_pr_body("Fixes #888\nCloses #876\nresolves #877\nSee also #999 for context")
    assert refs.issues == {888, 876, 877}, (
        "a bare #999 mention is not a closing reference and must not turn a pin green"
    )


def test_a_full_issue_url_closing_reference_is_read_too() -> None:
    refs = parse_pr_body(f"Fixes {ISSUE}/884")
    assert refs.issues == set(), "a full URL reference carries its own repo, not a bare number"
    assert refs.repo_issues == {("private_owner/music-dj-tools", 884)}


def test_an_owner_repo_shorthand_closing_reference_is_read_too() -> None:
    """GitHub's cross-repo shorthand `owner/repo#N` closes just like a full URL,
    but the reviewer's regex only knew the URL and bare-`#N` shapes; a merged
    PR using the shorthand left its pin fixed instead of merged (issue #914
    review, Wed 3 Sep 2026)."""
    refs = parse_pr_body("Fixes other/project#888")
    assert refs.issues == set(), "the shorthand carries its own repo, not a bare number"
    assert refs.repo_issues == {("other/project", 888)}


def test_an_empty_or_none_body_yields_nothing_rather_than_raising() -> None:
    for body in ("", None):
        refs = parse_pr_body(body)
        assert refs.pin_shas == {} and refs.issues == set()


# ----- select_merged_pins -------------------------------------------------
def test_a_listed_pin_id_is_selected_and_carries_its_sha() -> None:
    comments = [pin(), pin(id="other")]
    refs = parse_pr_body("pin b44c957f082f -> b63f6d7b")
    selected = select_merged_pins(comments, refs, THIS_REPO)
    assert [(s.id, s.sha) for s in selected] == [("b44c957f082f", "b63f6d7b")]


def test_a_pin_whose_issue_the_pr_closes_is_selected() -> None:
    comments = [
        pin(id="7c0c0167cb0a", issue_url=f"{ISSUE}/888"),
        pin(id="cb913aa5603e", issue_url=f"{ISSUE}/876", status="issued"),
        pin(id="unlinked"),
    ]
    selected = select_merged_pins(comments, parse_pr_body("Fixes #888"), THIS_REPO)
    assert [s.id for s in selected] == ["7c0c0167cb0a"]
    assert selected[0].sha is None, "a Fixes reference carries no commit sha"


def test_a_pin_whose_issue_a_same_repo_full_url_closes_is_selected() -> None:
    comments = [pin(id="7c0c0167cb0a", issue_url=f"{ISSUE}/888")]
    refs = parse_pr_body(f"Fixes {ISSUE}/888")
    selected = select_merged_pins(comments, refs, THIS_REPO)
    assert [s.id for s in selected] == ["7c0c0167cb0a"]


def test_a_full_issue_url_closing_a_different_repo_does_not_match_this_repos_issue() -> None:
    # Closing other/project#888 must not turn on this repo's pin for issue 888:
    # the numeric id is the same, the repo is not.
    comments = [pin(id="7c0c0167cb0a", issue_url=f"{ISSUE}/888")]
    refs = parse_pr_body("Fixes https://github.com/other/project/issues/888")
    assert select_merged_pins(comments, refs, THIS_REPO) == []


def test_a_bare_closing_reference_does_not_match_a_pin_filed_against_another_repo() -> None:
    # "Fixes #888" in THIS repo's PR is unambiguous by GitHub's own semantics -
    # it can only close an issue in THIS repo - so a pin whose issue_url points
    # at a DIFFERENT repo's #888 must not be swept up by the bare number alone.
    comments = [pin(id="7c0c0167cb0a", issue_url="https://github.com/other/project/issues/888")]
    refs = parse_pr_body("Fixes #888")
    assert select_merged_pins(comments, refs, THIS_REPO) == []


def test_an_issue_number_never_matches_by_substring() -> None:
    # /issues/88 must not be closed by "Fixes #888".
    comments = [pin(id="a", issue_url=f"{ISSUE}/88")]
    assert select_merged_pins(comments, parse_pr_body("Fixes #888"), THIS_REPO) == []


def test_already_merged_or_archived_pins_are_skipped_so_a_rerun_is_a_noop() -> None:
    comments = [
        pin(id="a", issue_url=f"{ISSUE}/888", status="merged"),
        pin(id="b", issue_url=f"{ISSUE}/888", status="archived"),
        pin(id="c", issue_url=f"{ISSUE}/888", status="fixed"),
    ]
    selected = select_merged_pins(comments, parse_pr_body("Fixes #888"), THIS_REPO)
    assert [s.id for s in selected] == ["c"]


def test_a_pin_selected_by_both_routes_appears_once_and_keeps_its_sha() -> None:
    comments = [pin(id="7c0c0167cb0a", issue_url=f"{ISSUE}/888")]
    refs = parse_pr_body("Fixes #888\npin 7c0c0167cb0a -> deadbeef")
    selected = select_merged_pins(comments, refs, THIS_REPO)
    assert len(selected) == 1 and selected[0].sha == "deadbeef"


# ----- merged_note --------------------------------------------------------
def test_the_note_appends_rather_than_replacing_the_agents_explanation() -> None:
    note = merged_note("Fixed in b63f6d7b on branch af--pin-review-2sep.", 890, "b63f6d7b")
    assert note.startswith("Fixed in b63f6d7b on branch")
    assert note.endswith("Merged in PR #890 (b63f6d7b)")


def test_a_pin_with_no_note_gets_only_the_merge_line() -> None:
    assert merged_note(None, 890, None) == "Merged in PR #890"


def test_appending_twice_changes_nothing() -> None:
    once = merged_note("Fixed in b63f6d7b.", 890, "b63f6d7b")
    assert merged_note(once, 890, "b63f6d7b") == once, (
        "if a re-run doubles the note then the merge lane cannot be re-driven safely"
    )


# ----- main: a configured daemon dropping out of discovery ----------------
def _stub_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    reachable = Target(machine="here-dev", base="http://127.0.0.1:9999", ssh_host=None)
    monkeypatch.setattr("scripts.pin_mark_merged._discover", lambda *a, **k: [reachable])
    monkeypatch.setattr("scripts.pin_mark_merged._mark_on_daemon", lambda *a, **k: 1)
    monkeypatch.setattr("scripts.pin_mark_merged._gh_repo", lambda: THIS_REPO)
    monkeypatch.setattr("scripts.pin_mark_merged._env_ports", list)


def _body_path(tmp_path) -> str:
    path = tmp_path / "body.md"
    path.write_text("pin b44c957f082f -> b63f6d7b\n", encoding="utf-8")
    return str(path)


def test_an_unreachable_configured_url_fails_the_run_even_though_another_daemon_marked(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_discovery(monkeypatch)
    body = _body_path(tmp_path)
    result = main(["914", "--body", body, "--url", "http://127.0.0.1:1", "--skip-silver"])
    assert result == 1
    assert "configured daemon http://127.0.0.1:1 unreachable" in capsys.readouterr().out


def test_when_every_configured_daemon_is_reachable_the_run_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _stub_discovery(monkeypatch)
    body = _body_path(tmp_path)
    assert main(["914", "--body", body, "--skip-silver"]) == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
