"""Sparse linked worktrees: the post-checkout hook and its policy (OPS-45).

Real repositories in tmp_path, the real hook through core.hooksPath, no mocks.

  - [if] plain `git worktree add` runs [then] excluded media is absent and `S`, [else stop].
  - [if] the primary checks out, or MDT_FULL_WORKTREE=1 [then] the tree is full, [else stop].
  - [if] a worktree restored to full switches branch [then] it stays full, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import sparse_worktree as sw
from tests.scripts.sparse_worktree_fixtures import (
    EXCLUDED,
    KEPT,
    add_worktree,
    build_primary,
    git,
    on_disk,
    tags,
)

pytestmark = pytest.mark.requirement("OPS-45")

NULL = "0" * 40


@pytest.fixture
def primary(tmp_path: Path) -> Path:
    return build_primary(tmp_path)


def _assert_full(repo: Path) -> None:
    assert set(EXCLUDED) | set(KEPT) <= on_disk(repo), "a tracked file is missing on disk"
    assert set(tags(repo).values()) == {"H"}, f"skip-worktree entries in a full tree: {tags(repo)}"
    assert not sw.is_sparse(repo)


# ----- the hook, end to end -----------------------------------------------------------


def test_plain_worktree_add_leaves_excluded_media_out(primary: Path, tmp_path: Path) -> None:
    """[if] plain `git worktree add` runs [then] excluded media is absent and S, [else stop]."""
    worktree = tmp_path / "wt-sparse"
    output = add_worktree(primary, worktree)
    present, status = on_disk(worktree), tags(worktree)
    assert not set(EXCLUDED) & present, f"excluded media materialized: {set(EXCLUDED) & present}"
    assert {status[rel] for rel in EXCLUDED} == {"S"}, status
    assert set(KEPT) <= present, f"kept paths missing: {set(KEPT) - present}"
    assert {status[rel] for rel in KEPT} == {"H"}, status
    assert f"SPARSE: {len(EXCLUDED)} media file(s) left out" in output, output


def test_primary_checkout_stays_full(primary: Path, tmp_path: Path) -> None:
    """[if] the primary checks out a branch or sees a null HEAD [then] it stays full, [else stop]."""
    add_worktree(primary, tmp_path / "wt-any")
    git(primary, "checkout", "-q", "-b", "other")
    assert sw.main(["--repo", str(primary), "post-checkout", NULL, "x" * 40, "1"]) == 0
    _assert_full(primary)


def test_opt_out_env_keeps_new_worktree_full(primary: Path, tmp_path: Path) -> None:
    """[if] MDT_FULL_WORKTREE=1 at `git worktree add` [then] the worktree is full, [else stop]."""
    worktree = tmp_path / "wt-full"
    add_worktree(primary, worktree, env={sw.CFG.OPT_OUT_ENV: "1"})
    _assert_full(worktree)


def test_opt_out_beats_sparse_patterns_git_inherits(primary: Path, tmp_path: Path) -> None:
    """[if] a worktree is added from a sparse one with the opt-out [then] it is full, [else stop]."""
    parent = tmp_path / "wt-parent"
    add_worktree(primary, parent)
    assert sw.is_sparse(parent)
    child = tmp_path / "wt-child"
    add_worktree(parent, child, env={sw.CFG.OPT_OUT_ENV: "1"})
    _assert_full(child)


def test_unknown_opt_out_value_is_refused_and_tree_left_full(primary: Path, tmp_path: Path) -> None:
    """[if] MDT_FULL_WORKTREE holds a junk value [then] a WARN names it, tree full, [else stop]."""
    worktree = tmp_path / "wt-junk"
    output = add_worktree(primary, worktree, env={sw.CFG.OPT_OUT_ENV: "yes"})
    assert "[WARN] sparse-worktree: MDT_FULL_WORKTREE='yes'" in output, output
    _assert_full(worktree)


def test_restored_worktree_stays_full_across_branch_switch(primary: Path, tmp_path: Path) -> None:
    """[if] a worktree restored to full switches branch [then] it stays full, [else stop]."""
    worktree = tmp_path / "wt-restored"
    add_worktree(primary, worktree)
    git(worktree, "sparse-checkout", "disable")
    _assert_full(worktree)
    git(worktree, "checkout", "-q", "-b", "wt-restored-next")
    _assert_full(worktree)


def test_full_and_sparse_commands_round_trip(primary: Path, tmp_path: Path) -> None:
    """[if] `full` then `sparse` run in a linked worktree [then] each state is exact, [else stop]."""
    worktree = tmp_path / "wt-cli"
    add_worktree(primary, worktree)
    assert sw.main(["--repo", str(worktree), "full"]) == 0
    _assert_full(worktree)
    assert sw.main(["--repo", str(worktree), "sparse"]) == 0
    assert {tags(worktree)[rel] for rel in EXCLUDED} == {"S"}


def test_sparse_command_refuses_the_primary(primary: Path) -> None:
    """[if] `sparse` is run in the primary checkout [then] it refuses by name, [else stop]."""
    with pytest.raises(sw.SparseCheckoutError, match="primary checkout"):
        sw.make_sparse(primary)
    _assert_full(primary)


# ----- policy -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("previous", "flag", "linked", "opt_out", "expected"),
    [
        (NULL, "1", True, False, sw.Action.SPARSE),
        ("0" * 64, "1", True, False, sw.Action.SPARSE),
        (NULL, "1", True, True, sw.Action.FULL),
        (NULL, "1", False, False, sw.Action.SKIP),
        ("a" * 40, "1", True, False, sw.Action.SKIP),
        ("0" * 39 + "1", "1", True, False, sw.Action.SKIP),
        (NULL, "0", True, False, sw.Action.SKIP),
    ],
)
def test_decide(previous: str, flag: str, linked: bool, opt_out: bool, expected: sw.Action) -> None:
    """[if] the hook's inputs vary [then] only a fresh linked add goes sparse, [else stop]."""
    assert sw.decide(previous, flag, linked=linked, opt_out=opt_out) is expected


# ----- readers ------------------------------------------------------------------------


def test_tracked_bytes_reads_skipped_entries_from_the_index(primary: Path, tmp_path: Path) -> None:
    """[if] a reader asks for skip-worktree paths [then] it gets the blob bytes, [else stop]."""
    sparse, full = tmp_path / "wt-s", tmp_path / "wt-f"
    add_worktree(primary, sparse)
    add_worktree(primary, full, env={sw.CFG.OPT_OUT_ENV: "1"})
    from_sparse = sw.tracked_bytes(sparse, sw.tracked_entries(sparse))
    from_full = sw.tracked_bytes(full, sw.tracked_entries(full))
    assert set(EXCLUDED) <= set(from_sparse), "a skipped entry produced no content"
    assert from_sparse == from_full


def test_require_materialized_names_skipped_paths(primary: Path, tmp_path: Path) -> None:
    """[if] a disk-only scope holds skipped paths [then] the refusal names them, [else stop]."""
    worktree = tmp_path / "wt-req"
    add_worktree(primary, worktree)
    with pytest.raises(sw.SparseCheckoutError, match=r"blog/next-hero-set/hero\.png") as raised:
        sw.require_materialized(worktree, [*KEPT, EXCLUDED[0]], purpose="probe")
    assert sw.CFG.RESTORE_COMMAND in str(raised.value)
    sw.require_materialized(worktree, list(KEPT), purpose="probe")


def test_tracked_bytes_keeps_reading_local_edits_from_disk(primary: Path, tmp_path: Path) -> None:
    """[if] a materialized file has a local edit [then] the reader sees the edit, [else stop]."""
    worktree = tmp_path / "wt-edit"
    add_worktree(primary, worktree)
    (worktree / "blog/post.md").write_text("edited\n")
    content = sw.tracked_bytes(worktree, sw.tracked_entries(worktree, "blog"))
    assert content["blog/post.md"] == b"edited\n"
    assert content["blog/next-hero-set/hero.png"].startswith(b"render notes"), "control"


def test_install_hook_copies_and_refuses_to_clobber(tmp_path: Path) -> None:
    """[if] install-hook meets a foreign post-checkout [then] it refuses, [else stop]."""
    repo = tmp_path / "clone"
    git(tmp_path, "init", "-q", str(repo))
    installed = sw.install_hook(repo)
    assert installed.read_bytes() == sw.CFG.HOOK_SOURCE.read_bytes()
    assert sw.install_hook(repo) == installed, "re-install of the same hook must be a no-op"
    installed.write_text("#!/bin/sh\necho someone else's hook\n")
    with pytest.raises(sw.SparseCheckoutError, match="different post-checkout"):
        sw.install_hook(repo)
    assert "someone else's hook" in installed.read_text()
