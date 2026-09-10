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
    _describe,
    _diff_paths,
    _git,
    _now,
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

    Deliberately NOT extended to a candidate's own parent (the state right
    before a path was first added, where it was implicitly absent):
    ``test_a_merge_resolution_deletion_already_landed_on_main_is_not_flagged``
    requires main to make the SAME deletion as an explicit commit before a
    resolution's deletion counts as landed, specifically so that "absence"
    only coexists via a deliberate main commit, never via the trivial fact
    that every path main ever added also has a pre-addition ancestor lacking
    it. Counting that pre-addition state would make ANY preview deletion of
    an ever-added path coexist by construction, defeating containment for
    deletions entirely; see r3974239193 for the rejected alternative and why.
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


def _union_diff_paths(cwd: Path, shas: list[str]) -> set[str]:
    """Union of ``_diff_paths(sha~1, sha)`` over every sha in ``shas``: the
    full set of paths some group of commits touched, shared by every
    combined-state check below so each unions its own class the same way.
    """
    paths: set[str] = set()
    for sha in shas:
        paths |= _diff_paths(cwd, f"{sha}~1", sha)
    return paths


def _resolution_carrying_merges(
    cwd: Path, main_ref: str, shas: list[str]
) -> tuple[list[str], set[str]]:
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
    keeps flagging it forever. Instead, isolate the paths that could carry
    content the human actually authored during the merge -- every path where
    merge_tree differs from EITHER parent, the UNION of the two per-parent
    diffs, not their intersection -- and check only those against main.

    The union matters, not just the intersection: a resolution can pick
    parent 0's content at one path and parent 1's content at ANOTHER path,
    so each individual path's diff against ONE parent is empty (it matches
    that parent verbatim) while the diff against the OTHER parent is not.
    The intersection of the two diff sets is then empty regardless of
    whether the resolution picked one parent WHOLESALE (genuinely safe) or
    picked different parents at different paths (a genuinely novel
    combination neither parent, and possibly no main commit, ever held
    together) -- an empty intersection cannot tell those two shapes apart.
    Skip only when merge_tree matches ONE parent's content across the
    ENTIRE union of touched paths: that is the one condition under which
    the resolution introduces no combination absent from a single existing
    tree. That works whether the merge resolved cleanly on top of a trivial
    auto-merge or was a real conflict with no auto-merge to diff against,
    because it never needs one: parents and the recorded merge_tree are
    always available.

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

    Returns the carrying shas AND, separately, the union of touched paths
    for every merge trusted here via the ``_paths_ever_together_on_main``
    branch specifically (not the wholesale-parent-match branch, which trusts
    a merge for a reason unrelated to main containment and so contributes
    nothing to it). ``evaluate()`` folds that set into the cross-class
    combined-state check alongside the other two classes' trusted paths.
    """
    main_trees: set[str] | None = None
    carrying: list[str] = []
    trusted_paths: set[str] = set()
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
            touched = _diff_paths(cwd, parents[0], merge_tree) | _diff_paths(
                cwd, parents[1], merge_tree
            )
            # Safe only when merge_tree matches ONE parent across the WHOLE
            # union: picking parent 0 at some paths and parent 1 at OTHER
            # paths can leave each path individually matching some parent
            # (so neither per-parent diff alone reveals it) while the
            # COMBINATION matches neither parent and may exist nowhere on
            # main. Not reachable with an empty union: `is_carrying` above
            # already required merge_tree to differ from a trivial replay
            # of the parents (or from a single legal parent for a genuine
            # conflict), so at least one parent's diff is always non-empty
            # here.
            picked_one_parent_wholesale = any(
                _trees_identical(cwd, merge_tree, parent, *touched) for parent in parents
            )
            if picked_one_parent_wholesale:
                continue
            if _paths_ever_together_on_main(cwd, main_ref, merge_tree, touched):
                trusted_paths |= touched
                continue
        else:
            if main_trees is None:
                main_trees = set(_git(cwd, "log", main_ref, "--format=%T").split())
            if merge_tree in main_trees:
                continue
        carrying.append(sha)
    return carrying, trusted_paths


def _mark_superseded_commits(
    cwd: Path, preview_ref: str, commits: list[PreviewOnlyCommit]
) -> None:
    """Flag a preview-only commit as ``superseded`` when its net effect is
    already gone from the served tree -- an explicit revert, or any later
    edit that happens to land back where it started. Scoped to exactly the
    paths that commit changed: if the preview's CURRENT tree already matches
    what came right before that commit at those paths, nothing of it remains.

    An EMPTY diff (an ``--allow-empty`` commit, or any commit whose net
    effect is a no-op) is the degenerate case of "nothing of it remains": it
    never served any content to begin with, so it is vacuously superseded.
    ``paths`` being empty must not fall through to the "" pathspec of
    ``_trees_identical``, which compares the ENTIRE tree rather than nothing
    -- the wrong, much broader question.
    """
    for commit in commits:
        paths = _diff_paths(cwd, f"{commit.sha}~1", commit.sha)
        if not paths or _trees_identical(cwd, f"{commit.sha}~1", preview_ref, *paths):
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
    _revoke_landed_if_combined_state_never_coexisted(cwd, main_ref, preview_ref, commits)


def _revoke_landed_if_combined_state_never_coexisted(
    cwd: Path, main_ref: str, preview_ref: str, commits: list[PreviewOnlyCommit]
) -> None:
    """Per-commit containment above is necessary but not sufficient: each
    commit's own touched paths can independently match SOME ``main_ref``
    commit at a DIFFERENT point in main's history, while their served state
    never coexisted TOGETHER on any one main commit -- main set path a,
    later dropped it, then separately set path b. Union every currently
    ``landed`` (and not ``superseded``) commit's touched paths and require
    them to coexist on one main commit; if they do not, none of them
    actually landed as a GROUP, so revoke the flag and let the ordinary
    age-based staleness filter (``Report.stale``) age them out on its own
    schedule -- the same grace period every other preview-only commit gets.
    """
    landed = [c for c in commits if c.landed and not c.superseded]
    if not landed:
        return
    paths = _union_diff_paths(cwd, [c.sha for c in landed])
    if paths and not _paths_ever_together_on_main(cwd, main_ref, preview_ref, paths):
        for commit in landed:
            commit.landed = False


def _newest_touch_age_hours(cwd: Path, preview_ref: str, paths: set[str]) -> float:
    """How long ago the union's CURRENTLY-SERVED combination came into
    being: the age of whichever commit MOST RECENTLY touched any of
    ``paths`` on ``preview_ref``, not any specific historical commit's own
    age.

    A path's original contributor can be entirely superseded by a later
    edit to that SAME path -- a fresh preview-only commit overwriting what
    an old, already-''-''-classified commit once set -- and that later edit
    is what the preview is actually serving; the containment check above
    already tests against ``preview_ref``'s CURRENT tree for exactly this
    reason. Gating the age on a stale contributor's own commit date, instead
    of on the most recent touch, blames a commit that no longer contributes
    to the current combination and can fire immediately for a state that in
    fact just formed and has not had its grace period yet.
    """
    last_touch_shas = {
        _git(cwd, "log", "-1", "--format=%H", preview_ref, "--", path).strip() for path in paths
    }
    last_touch_shas.discard("")
    if not last_touch_shas:
        return 0.0
    described = _describe(cwd, sorted(last_touch_shas))
    return min(
        (_now() - described[sha][0]).total_seconds() / 3600.0 for sha in last_touch_shas
    )


def _patch_equivalent_never_coexisted(
    cwd: Path, main_ref: str, preview_ref: str, landed_shas: list[str], max_age_hours: int
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

    Gated on age like every other preview-only finding, but on the age of
    whichever commit most recently touched the union of paths (see
    ``_newest_touch_age_hours``), not on ``landed_shas``' own commit dates:
    an old, long-``-``-classified commit whose path was since overwritten by
    a fresh preview-only edit is no longer what is being served there, so
    blaming ITS age would fire on a combination that in fact just formed and
    has not overstayed the ``max_age_hours`` grace period yet.
    """
    paths = _union_diff_paths(cwd, landed_shas)
    if not paths or _paths_ever_together_on_main(cwd, main_ref, preview_ref, paths):
        return None
    if _newest_touch_age_hours(cwd, preview_ref, paths) <= max_age_hours:
        return None
    return (
        f"{len(landed_shas)} patch-equivalent commit(s) each matched "
        f"{main_ref} individually via git cherry, but their combined "
        f"touched paths never coexisted on a single {main_ref} commit "
        f"(e.g. one landed and was later reverted): the preview is "
        f"serving content no main commit ever held together"
    )


def _cross_class_never_coexisted(
    cwd: Path,
    main_ref: str,
    preview_ref: str,
    paths: set[str],
    max_age_hours: int,
) -> str | None:
    """The three checks above each union paths WITHIN one class -- a merge
    resolution's own touched paths, a group of ``landed`` '+' commits'
    touched paths, a group of patch-equivalent '-' commits' touched paths --
    and require THAT union to coexist on one ``main_ref`` commit. None of
    them unions ACROSS classes. A preview can serve one path whose value is
    explained by a trusted merge resolution or squashed '+' commit and a
    DIFFERENT path explained by a trusted '-' patch-equivalent commit, with
    neither class's own check ever seeing the other's path: main can hold
    (a=2, b=0) at one commit and (a=0, b=1) at a later one, never both
    together, while each class's own per-class check independently reports
    clean because `a` alone coexists somewhere on main and `b` alone
    coexists somewhere else. ``evaluate()`` calls this once, after every
    class has finished its own per-class marking and revoking, with the
    union of every path any class STILL currently trusts, so it is the one
    place that can see a combination no single main commit ever held
    together even though every class-level check passed.

    Gated on ``_newest_touch_age_hours`` over this CROSS-CLASS union, not
    any one class's own union, for the same reason every other finding here
    is: the combination currently served can be fresher than any single
    contributing commit's own age.
    """
    if not paths or _paths_ever_together_on_main(cwd, main_ref, preview_ref, paths):
        return None
    if _newest_touch_age_hours(cwd, preview_ref, paths) <= max_age_hours:
        return None
    return (
        f"{len(paths)} path(s) are each individually trusted as landed by a "
        f"different mechanism (merge resolution, squashed commit, or "
        f"patch-equivalent commit), but their combined state never "
        f"coexisted on a single {main_ref} commit: the preview is serving a "
        f"combination no main commit ever held together"
    )
