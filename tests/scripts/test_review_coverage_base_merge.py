"""Base-merge carry for reviewer coverage (REVIEW-16): the disjoint-paths rule.

Real throwaway git repositories in tmp_path: real commits, real merges, a real
bare `origin` whose main is pinned by `ls-remote`, real merge-bases and diffs.
No mocks of git. Unmeasurable cases and triage output live in
test_review_coverage_base_merge_unknown.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.review_coverage_base_merge import changed_paths, net_diff, single_merge_base
from tests.scripts.base_merge_repo import (
    FILE,
    OTHER,
    QUOTED,
    THIRD,
    advance_main,
    assert_no_carry,
    attempt,
    commit,
    git,
    lines,
    make_repo,
    make_reviewed,
    merge_main,
    merge_main_with_edit,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


@pytest.fixture
def reviewed(repo: Path) -> str:
    return make_reviewed(repo)


# ----------------------------------------------------------------------------
# positive controls: main changed only unrelated paths, the merge is clean


@pytest.mark.requirement("REVIEW-16")
def test_clean_base_merge_of_unrelated_file_carries_with_proof(repo: Path, reviewed: str) -> None:
    """[if] main changes only an unrelated file and the merge is clean [then] coverage carries, [else stop]."""
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main(repo)

    carried = attempt(repo, reviewed, head).verdict
    assert carried is not None and carried.reviewed is True
    assert carried.reason == f"carried from {reviewed[:11]} (net diff unchanged since; base merge only)"
    assert carried.carried_from == reviewed
    proof = "\n".join(carried.carry_proof)
    assert f"reviewed {reviewed} -> head {head}" in proof
    assert "disjoint paths: main changed 1, PR changed 1, shared 0" in proof


@pytest.mark.requirement("REVIEW-16")
def test_quoted_path_is_read_raw_and_carries(repo: Path) -> None:
    """[if] the PR's file name has spaces and non-ASCII [then] its path reads raw and coverage carries, [else stop]."""
    reviewed = commit(repo, QUOTED, "café v1\n")
    base = git(repo, "merge-base", "main", reviewed)
    assert changed_paths(repo, base, reviewed) == frozenset({QUOTED.encode()})
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main(repo)

    assert attempt(repo, reviewed, head).verdict is not None


# ----------------------------------------------------------------------------
# condition 2: main and the PR share no path


@pytest.mark.requirement("REVIEW-16")
def test_main_change_to_pr_file_far_from_hunk_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] main edits a file the PR touches, far from the PR's hunk [then] no carry, [else stop].

    The disjoint-paths trade-off: this used to carry under patch-id. It is safe
    only when the reviewed lines provably kept their meaning, which nothing but
    an untouched file proves.
    """
    advance_main(repo, FILE, lines({2: "line2 main"}))
    head = merge_main(repo)
    assert git(repo, "show", f"{head}:{FILE}").splitlines()[2] == "line2 main"

    assert_no_carry(repo, reviewed, head)


@pytest.mark.requirement("REVIEW-16")
def test_main_change_to_quoted_pr_path_does_not_carry(repo: Path) -> None:
    """[if] main edits the PR's spaced, non-ASCII path [then] the overlap is seen and no carry, [else stop]."""
    git(repo, "checkout", "-q", "main")
    commit(repo, QUOTED, lines())
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "-B", "pr")
    reviewed = commit(repo, QUOTED, lines({30: "line30 pr"}))
    advance_main(repo, QUOTED, lines({2: "line2 main"}))
    head = merge_main(repo)
    tip = git(repo, "rev-parse", "origin/main")
    main_paths = changed_paths(repo, single_merge_base(repo, tip, reviewed), single_merge_base(repo, tip, head))
    assert main_paths == frozenset({QUOTED.encode()}), "the probe must name the overlapping raw path"

    assert_no_carry(repo, reviewed, head)


# ----------------------------------------------------------------------------
# adversarial cases from the patch-id rounds, kept as negatives

#: Seven lines whose 3-line-context window is identical wherever the block sits.
_DUP_BLOCK = {0: "alpha", 1: "beta", 2: "gamma", 3: "val = 10", 4: "other = 20", 5: "delta", 6: "epsilon"}


def _duplines(first_block_val: str) -> str:
    edits = dict(_DUP_BLOCK)
    edits[3] = first_block_val
    edits.update({10 + offset: text for offset, text in _DUP_BLOCK.items()})
    return lines(edits)


def _dup_lines_block_a_removed() -> str:
    edits = {i: f"removed{i}" for i in range(7)}
    edits.update({10 + offset: text for offset, text in _DUP_BLOCK.items()})
    return lines(edits)


@pytest.mark.requirement("REVIEW-16")
def test_relocated_hunk_onto_duplicate_block_does_not_carry(repo: Path) -> None:
    """[if] a merge ports the reviewed edit onto an identical duplicate block [then] no carry, [else stop].

    Sol BLOCKING P1 #2 on PR #4599: equal patch-ids, different block.
    """
    git(repo, "checkout", "-q", "main")
    commit(repo, FILE, _duplines("val = 10"))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "-B", "pr")
    reviewed = commit(repo, FILE, _duplines("val = 999"))
    advance_main(repo, FILE, _dup_lines_block_a_removed())
    ported = _dup_lines_block_a_removed().split("\n")
    ported[13] = "val = 999"
    head = merge_main_with_edit(repo, FILE, "\n".join(ported))

    assert_no_carry(repo, reviewed, head)


@pytest.mark.requirement("REVIEW-16")
def test_edit_ported_to_a_unique_block_main_moved_does_not_carry(repo: Path) -> None:
    """[if] main moves the sole matching block and the merge ports the edit there [then] no carry, [else stop].

    Sol BLOCKING P1 #3 on PR #4599: the pre-image is unique at both bases, so
    the uniqueness check carried it.
    """
    func_a = ["def a():", "    x = 1", "    y = 2", "    z = 3", "    return x"]
    func_b = ["def b():", "    pass"]
    git(repo, "checkout", "-q", "main")
    commit(repo, FILE, "\n".join(func_a + func_b) + "\n")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "-B", "pr")
    reviewed = commit(repo, FILE, "\n".join(func_a).replace("y = 2", "y = 99") + "\n" + "\n".join(func_b) + "\n")
    moved = ["def a():", "    return 0", "def b():", "    x = 1", "    y = 2", "    z = 3", "    return x"]
    advance_main(repo, FILE, "\n".join(moved) + "\n")
    head = merge_main_with_edit(repo, FILE, "\n".join(moved).replace("y = 2", "y = 99") + "\n")

    assert_no_carry(repo, reviewed, head)


@pytest.mark.requirement("REVIEW-16")
def test_hostile_diff_config_does_not_change_the_net_diff(repo: Path, reviewed: str) -> None:
    """[if] diff.context=0, diff.noprefix or core.abbrev is set [then] net diff bytes are unchanged, [else stop]."""
    base = git(repo, "merge-base", "main", reviewed)
    pinned = net_diff(repo, base, reviewed)
    for key, value in (
        ("diff.context", "0"),
        ("diff.noprefix", "true"),
        ("diff.mnemonicPrefix", "true"),
        ("diff.relative", "true"),
        ("diff.external", "false"),
        ("diff.submodule", "log"),
        ("color.ui", "always"),
        ("core.abbrev", "4"),
    ):
        git(repo, "config", key, value)

    assert net_diff(repo, base, reviewed) == pinned


@pytest.mark.requirement("REVIEW-16")
def test_context_zero_config_does_not_mask_a_hand_edit_beside_the_hunk(repo: Path, reviewed: str) -> None:
    """[if] diff.context=0 is set and the merge edits a context line of the hunk [then] no carry, [else stop]."""
    git(repo, "config", "diff.context", "0")
    git(repo, "config", "diff.noprefix", "true")
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main_with_edit(repo, FILE, lines({30: "line30 pr", 28: "line28 sneaky"}))

    assert_no_carry(repo, reviewed, head)


# ----------------------------------------------------------------------------
# condition 3: the merge introduced no hand edits


@pytest.mark.requirement("REVIEW-16")
def test_whitespace_hand_edit_to_pr_line_inside_merge_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] a clean-path merge re-indents a reviewed line [then] no carry, [else stop]."""
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main_with_edit(repo, FILE, lines({30: "    line30 pr"}))

    assert_no_carry(repo, reviewed, head)


@pytest.mark.requirement("REVIEW-16")
def test_hand_edit_to_untouched_file_inside_merge_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] the merge commit edits a file neither main nor the PR touched [then] no carry, [else stop]."""
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main_with_edit(repo, THIRD, "third smuggled\n")

    assert_no_carry(repo, reviewed, head)


@pytest.mark.requirement("REVIEW-16")
def test_hand_edit_to_main_changed_file_inside_merge_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] the merge commit edits a file main changed, beyond main's version [then] no carry, [else stop]."""
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main_with_edit(repo, OTHER, "other v2 smuggled\n")

    assert_no_carry(repo, reviewed, head)


# ----------------------------------------------------------------------------
# condition 1: base merges only, onto the PR's own first-parent chain


@pytest.mark.requirement("REVIEW-16")
def test_own_commit_on_pr_file_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] a commit after review changes the PR's own file [then] no carry, [else stop]."""
    commit(repo, FILE, lines({30: "line30 pr", 31: "line31 pr"}))
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main(repo)

    assert_no_carry(repo, reviewed, head)


@pytest.mark.requirement("REVIEW-16")
def test_revert_pair_then_base_merge_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] own commits that net to nothing ride along with a base merge [then] no carry, [else stop]."""
    commit(repo, FILE, lines({30: "line30 pr", 5: "line5 sneaky"}))
    commit(repo, FILE, lines({30: "line30 pr"}))
    advance_main(repo, OTHER, "other v2\n")
    head = merge_main(repo)

    assert_no_carry(repo, reviewed, head)


@pytest.mark.requirement("REVIEW-16")
def test_merge_with_main_as_first_parent_does_not_carry(repo: Path, reviewed: str) -> None:
    """[if] the head is main merged with the PR, main as first parent [then] no carry, [else stop]."""
    advance_main(repo, OTHER, "other v2\n")
    git(repo, "checkout", "-q", "-B", "pr", "main")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", reviewed)
    head = git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", f"{head}^1") == git(repo, "rev-parse", "main")

    assert_no_carry(repo, reviewed, head)


@pytest.mark.requirement("REVIEW-16")
def test_gitlink_smuggled_into_merge_does_not_carry_when_submodules_are_ignored(repo: Path, reviewed: str) -> None:
    """[if] a merge adds a submodule pointer while diff.ignoreSubmodules=all [then] no carry, [else stop]."""
    git(repo, "config", "diff.ignoreSubmodules", "all")
    advance_main(repo, OTHER, "other v2\n")
    git(repo, "merge", "-q", "--no-commit", "main")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{reviewed},vendor/sub")
    git(repo, "commit", "-q", "--no-edit")
    head = git(repo, "rev-parse", "HEAD")
    assert git(repo, "ls-tree", head, "--", "vendor/sub").startswith("160000 commit"), "gitlink must be in the merge"

    assert_no_carry(repo, reviewed, head)
