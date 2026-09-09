"""Tree-containment checks for the preview-branch drift check (DEVOPS-05).

Everything ``git cherry``'s per-commit patch-id comparison cannot see on its
own: a multi-commit topic main lands as one squash, a merge whose conflict
resolution carries content no ordinary commit records, and a patch-equivalent
commit main later reverted. Built on the git plumbing and model in
``scripts/preview_drift_git.py``; ``scripts/preview_drift_core.py`` builds
``evaluate()`` on top of both, so no file in the set crosses the 600-line
file-size ratchet.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.preview_drift_git import (
    MeasurementError,
    PreviewOnlyCommit,
    _diff_paths,
    _git,
    _trees_identical,
)


def _paths_ever_together_on_main(
    cwd: Path, main_ref: str, treeish: str, paths: set[str]
) -> bool:
    """True when some SINGLE commit on ``main_ref`` matches ``treeish`` at
    EVERY one of ``paths`` SIMULTANEOUSLY.

    Checking each path's presence on main independently is not this test:
    main can hold each path's matching content at a different, non-
    overlapping commit and never once hold them all together, so the
    preview serves multi-file state no main commit ever contained. This is
    the ONE notion of containment shared by every call site (a merge
    resolution's touched paths, a preview-only commit's own touched paths,
    and a set of patch-equivalent commits' combined touched paths) -- tree
    containment at a set of paths, searched across main's full HISTORY
    rather than its current tip, so a resolution main landed and later
    edited further still counts.

    Content at a path is constant between commits that change it, so the
    only commits worth checking are ones that changed at least one of
    ``paths``; ``git log`` with a pathspec finds exactly those in one call
    regardless of history length. ``--full-history`` is required rather than
    the default history simplification, which can skip a merge commit that
    changed a path relative to only one of its parents.
    """
    candidates = _git(
        cwd, "log", main_ref, "--full-history", "--format=%H", "--", *sorted(paths)
    ).split()
    return any(_trees_identical(cwd, treeish, sha, *paths) for sha in candidates)


def _merge_commits(cwd: Path, main_ref: str, preview_ref: str) -> list[str]:
    """Merge commits reachable from the preview and not from main.

    ``git cherry`` never reports these: it walks with ``--no-merges``.
    """
    return _git(cwd, "rev-list", "--merges", f"{main_ref}..{preview_ref}").split()


def _resolution_carrying_merges(cwd: Path, main_ref: str, shas: list[str]) -> list[str]:
    """Merges whose recorded tree is NOT what merging their parents produces,
    AND whose resolved content main has not since gained some other way.

    A conflict resolution (or a hand edit made during a merge) is content that
    exists on no ordinary commit, so patch-id comparison cannot see it at all:
    the preview can serve code main has never had while every per-commit row
    says clean. Replaying the merge in the object store is the exact test, and
    it is a NEGATIVE control by construction -- a merge that resolves trivially
    reproduces its own tree, so the ordinary "merge main into preview" commit
    is not flagged and this cannot degenerate into flagging every merge.

    That first test alone still over-reports once main independently catches
    up: if main later gains the resolved content, it is no longer preview-only,
    even though replaying the two parents still fails to reproduce it (parents
    are fixed history; whether main separately caught up is not visible from
    them). A whole-tree hash comparison is not that test: main can land the
    resolution bundled with any unrelated change in the SAME commit, which
    changes that commit's whole-tree hash even though every path the
    resolution touched is byte-identical on main, so a %T membership test
    keeps flagging it forever. Instead, isolate the paths where merge_tree
    differs from BOTH parents -- content the human actually authored during
    the merge, not simply inherited from one side -- and check only those
    against main. That works whether the merge resolved cleanly on top of a
    trivial auto-merge or was a real conflict with no auto-merge to diff
    against, because it never needs one: parents and the recorded merge_tree
    are always available.

    "Against main" means main's whole HISTORY at the touched paths, not its
    current tip: if main lands the resolution and later edits one of those
    paths again, the tip no longer matches, but the resolved content still
    reached main at some point and must not re-age into a false DRIFT. And
    the touched paths must be checked TOGETHER, in one main tree, not one at
    a time: main can hold each path's matching content at a different,
    non-overlapping commit and never once hold them all together, which an
    independent per-path history search cannot tell apart from genuine
    containment. ``_paths_ever_together_on_main`` is that single shared test;
    the preview-only-commit case below uses the same one.
    """
    main_trees: set[str] | None = None
    carrying: list[str] = []
    for sha in shas:
        merge_tree = _git(cwd, "rev-parse", f"{sha}^{{tree}}").strip()
        parents = _git(cwd, "rev-parse", f"{sha}^@").split()
        if len(parents) != 2:
            # An octopus merge cannot be replayed pairwise. That is a failed
            # measurement of that commit, not a clean bill of health, so it
            # is a carrying candidate rather than assumed innocent.
            is_carrying = True
        else:
            proc = subprocess.run(
                ["git", "merge-tree", "--write-tree", parents[0], parents[1]],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 1:
                # The parents conflict, so whatever tree was recorded is a
                # human resolution and exists nowhere else -- yet.
                is_carrying = True
            elif proc.returncode != 0:
                raise MeasurementError(
                    f"git merge-tree failed for {sha} in {cwd} "
                    f"(exit {proc.returncode}): {proc.stderr.strip()}"
                )
            else:
                replayed = proc.stdout.split("\n", 1)[0].strip()
                if not replayed:
                    raise MeasurementError(f"git merge-tree printed no tree for {sha}")
                is_carrying = replayed != merge_tree
        if not is_carrying:
            continue
        if len(parents) == 2:
            touched = _diff_paths(cwd, parents[0], merge_tree) & _diff_paths(
                cwd, parents[1], merge_tree
            )
            # Empty touched means every path in the merge tree matches at
            # least one parent verbatim: the resolution picked a side, path
            # by path, and introduced no content that is not already in one
            # of the parents. That is NOT evidence of preview-only content,
            # so it must not fall through to `carrying.append(sha)` below.
            if not touched:
                continue
            if _paths_ever_together_on_main(cwd, main_ref, merge_tree, touched):
                continue
        else:
            if main_trees is None:
                main_trees = set(_git(cwd, "log", main_ref, "--format=%T").split())
            if merge_tree in main_trees:
                continue
        carrying.append(sha)
    return carrying


def _mark_superseded_commits(
    cwd: Path, preview_ref: str, commits: list[PreviewOnlyCommit]
) -> None:
    """Flag a preview-only commit as ``superseded`` when its net effect is
    already gone from the served tree -- an explicit revert, or any later
    edit that happens to land back where it started. Scoped to exactly the
    paths that commit changed: if the preview's CURRENT tree already matches
    what came right before that commit at those paths, nothing of it remains.
    """
    for commit in commits:
        paths = _diff_paths(cwd, f"{commit.sha}~1", commit.sha)
        if paths and _trees_identical(cwd, f"{commit.sha}~1", preview_ref, *paths):
            commit.superseded = True


def _mark_landed_commits(
    cwd: Path, main_ref: str, preview_ref: str, commits: list[PreviewOnlyCommit]
) -> None:
    """Flag a preview-only commit as ``landed`` when the paths IT touched
    match, TOGETHER, some single commit on ``main_ref`` -- even though the
    tips as a whole differ. This is the squash case: main lands a
    multi-commit preview topic as one squash commit, then takes any
    unrelated commit afterward, and the tips stop matching even though every
    original topic commit's content reached main together in that one
    squash tree. Scoped to exactly the paths that commit changed, and
    checked with the same single-tree containment test a merge resolution
    uses (``_paths_ever_together_on_main``), so a preview-only commit and a
    merge resolution share one notion of "landed" rather than two.
    """
    for commit in commits:
        paths = _diff_paths(cwd, f"{commit.sha}~1", commit.sha)
        if paths and _paths_ever_together_on_main(cwd, main_ref, preview_ref, paths):
            commit.landed = True


def _patch_equivalent_never_coexisted(
    cwd: Path, main_ref: str, preview_ref: str, landed_shas: list[str]
) -> str | None:
    """``git cherry``'s ``-`` rows match ONE commit's patch id at a time and
    are trusted as landed forever, with no later re-check: a commit that
    landed on ``main_ref`` and was LATER REVERTED still reads ``-``, and an
    unrelated second commit cherry-picked afterward does not revisit it.
    Union every such commit's own touched paths and require them to
    coexist, TOGETHER, on one ``main_ref`` commit -- the same containment
    test the squash-landing and merge-resolution cases share -- so a preview
    that serves two patch-equivalent commits whose combined state no main
    commit ever held (main dropped the first before landing the second) is
    still caught.
    """
    paths: set[str] = set()
    for sha in landed_shas:
        paths |= _diff_paths(cwd, f"{sha}~1", sha)
    if paths and not _paths_ever_together_on_main(cwd, main_ref, preview_ref, paths):
        return (
            f"{len(landed_shas)} patch-equivalent commit(s) each matched "
            f"{main_ref} individually via git cherry, but their combined "
            f"touched paths never coexisted on a single {main_ref} commit "
            f"(e.g. one landed and was later reverted): the preview is "
            f"serving content no main commit ever held together"
        )
    return None
