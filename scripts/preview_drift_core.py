"""Measurement orchestration for the preview-branch drift check (DEVOPS-05).

``evaluate()``, built on top of the git plumbing and model in
``scripts/preview_drift_git.py`` and the containment checks in
``scripts/preview_drift_containment.py``. Split three ways out of
``scripts/preview_drift_check.py`` so no file in the set crosses the 600-line
file-size ratchet. The CLI module owns argument parsing, rendering, and
``main()``; everything it needs from here it imports explicitly, so
``python -m scripts.preview_drift_check`` and
``import scripts.preview_drift_check`` are unaffected by this split.
"""
from __future__ import annotations

from pathlib import Path

from scripts.preview_drift_containment import (
    _cross_class_never_coexisted,
    _mark_landed_commits,
    _mark_superseded_commits,
    _merge_commits,
    _patch_equivalent_never_coexisted,
    _resolution_carrying_merges,
    _union_diff_paths,
)
from scripts.preview_drift_git import (
    DEFAULT_MAX_AGE_HOURS,
    DEFAULT_MAX_BEHIND,
    FOSSIL_THRESHOLD,
    MeasurementError,
    PreviewOnlyCommit,
    Report,
    _describe,
    _git,
    _rev_parse,
    _trees_identical,
    _worktree_dirty,
    fetch_origin,
)

__all__ = [
    "DEFAULT_MAX_AGE_HOURS",
    "DEFAULT_MAX_BEHIND",
    "FOSSIL_THRESHOLD",
    "MeasurementError",
    "Report",
    "evaluate",
    "fetch_origin",
]


def _apply_never_coexisted_check(
    cwd: Path, main_ref: str, preview_ref: str, report: Report, landed_shas: list[str]
) -> None:
    """Wrap ``_patch_equivalent_never_coexisted`` with its two guard
    conditions (identical trees need no check; no '-' rows means nothing to
    union) as a single call, so ``evaluate()`` carries one decision point
    here instead of two nested ones.
    """
    if report.serves_main_tree or not landed_shas:
        return
    reason = _patch_equivalent_never_coexisted(
        cwd, main_ref, preview_ref, landed_shas, report.max_age_hours
    )
    if reason:
        report.verdict = "DRIFT"
        report.reasons.append(reason)


def _apply_cross_class_never_coexisted_check(
    cwd: Path,
    main_ref: str,
    preview_ref: str,
    report: Report,
    landed_shas: list[str],
    merge_trusted_paths: set[str],
) -> None:
    """Union the paths every currently-trusted class still relies on --
    ``landed`` (and not ``superseded``) '+' commits, patch-equivalent '-'
    commits, and merges trusted via main containment -- and run ONE
    cross-class ``_cross_class_never_coexisted`` check over the combination.
    Each class's own check (called separately, before this) only unions
    within its own class, so a combination that mixes surviving content from
    TWO different classes can pass every one of them independently; this is
    the single check that sees the union across all three.
    """
    if report.serves_main_tree:
        return
    landed_paths = _union_diff_paths(
        cwd, [c.sha for c in report.preview_only if c.landed and not c.superseded]
    )
    patch_equivalent_paths = _union_diff_paths(cwd, landed_shas)
    combined = landed_paths | patch_equivalent_paths | merge_trusted_paths
    reason = _cross_class_never_coexisted(
        cwd, main_ref, preview_ref, combined, report.max_age_hours
    )
    if reason:
        report.verdict = "DRIFT"
        report.reasons.append(reason)


def _promote_to_fossil(report: Report) -> None:
    """Escalate ``report.verdict`` to FOSSIL, unless the ref being measured
    is the authoritative live worktree, in which case DRIFT is preserved
    instead.

    FOSSIL means "stray, non-authoritative ref: safe to re-point or delete,
    not unmerged work". In ``--worktree`` mode ``cwd`` IS the served
    directory (module docstring on ``scripts/preview_drift_check.py``), so a
    fossil-shaped commit history at HEAD is STILL the live head being
    served right now, committed or not -- saying "safe to delete" about code
    currently being served is actively wrong (r3974540460). This subsumes
    the earlier, narrower "dirty worktree" guard: an uncommitted edit was
    one way live content could be masked by FOSSIL, but any worktree
    measurement is authoritative regardless of whether its content is
    committed or merely on disk. Only a non-authoritative ``--remote``
    measurement, which cannot see anything live at all, gets FOSSIL.
    """
    if report.mode == "worktree":
        report.verdict = "DRIFT"
    else:
        report.verdict = "FOSSIL"


def _fossil_shape_conclusion(mode: str) -> str:
    """The sentence explaining what a fossil-shaped commit history MEANS,
    which depends entirely on whether the measured ref is being served right
    now: a stray ref is safe to discard, but the authoritative worktree head
    never is, however fossil-shaped its history looks (r3974540460).
    """
    if mode == "worktree":
        return (
            "this IS the authoritative worktree head being served right "
            "now, so it is reported as DRIFT rather than dismissed as a "
            "stray ref safe to delete"
        )
    return "re-point or delete the ref: it is not unmerged preview work"


def evaluate(
    cwd: Path,
    preview_ref: str,
    main_ref: str,
    mode: str,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    max_behind: int = DEFAULT_MAX_BEHIND,
    fossil_threshold: int = FOSSIL_THRESHOLD,
) -> Report:
    """Measure one preview ref against one main ref. Raises MeasurementError."""
    report = Report(
        mode=mode,
        preview_ref=preview_ref,
        main_ref=main_ref,
        max_age_hours=max_age_hours,
        max_behind=max_behind,
    )
    report.preview_sha = _rev_parse(cwd, preview_ref)
    report.main_sha = _rev_parse(cwd, main_ref)

    # git cherry compares patch ids, so a commit that reached main by squash or
    # rebase is reported with '-' (equivalent found) and is NOT drift. Only '+'
    # lines are genuinely preview-only.
    cherry = _git(cwd, "cherry", main_ref, preview_ref)
    only_shas = [
        line.strip().split(maxsplit=1)[1]
        for line in cherry.splitlines()
        if line.strip().startswith("+")
    ]
    landed_shas = [
        line.strip().split(maxsplit=1)[1]
        for line in cherry.splitlines()
        if line.strip().startswith("-")
    ]
    described = _describe(cwd, only_shas)
    report.preview_only = [
        PreviewOnlyCommit(sha=sha, committed_at=described[sha][0], subject=described[sha][1])
        for sha in only_shas
    ]

    report.behind = int(
        _git(cwd, "rev-list", "--count", f"{preview_ref}..{main_ref}").strip()
    )

    # The TREE is the arbiter of what is actually being served. Both of the
    # blind spots below are blind spots of per-commit patch-id comparison, and
    # neither can be closed by looking at commits harder.
    report.serves_main_tree = _trees_identical(cwd, main_ref, preview_ref)

    # A second blind spot patch-id comparison cannot see on its own: a
    # preview-only commit can be followed by its own revert while main
    # independently advances, so the whole-tree check above cannot
    # short-circuit, yet none of that commit's content is still being
    # served.
    #
    # A third: main squashes a multi-commit preview topic into one commit
    # and then takes any unrelated commit, so the tips stop matching even
    # though every original topic commit's content reached main together in
    # that one squash tree.
    #
    # A fourth, on the OTHER side of git cherry's classification, is applied
    # further down by `_apply_never_coexisted_check`, after the FOSSIL checks
    # so a divergent-lineage verdict is not overwritten: a '-' row is trusted
    # as landed forever from a single patch-id match, with no later
    # re-check, so two such commits can each read "landed" while their
    # COMBINED state never coexisted on any one main commit.
    if not report.serves_main_tree:
        _mark_superseded_commits(cwd, preview_ref, report.preview_only)
        _mark_landed_commits(cwd, main_ref, preview_ref, report.preview_only)

    # A fifth blind spot neither commit nor tree comparison can see: in
    # --worktree mode, `cwd` IS the served directory, and a staged, unstaged,
    # or untracked edit on top of HEAD can be live in the browser while HEAD
    # equals main and every check above reports clean. `_promote_to_fossil`
    # below preserves DRIFT for any worktree measurement regardless of this
    # specific dirty-paths finding, so this DRIFT can never be overwritten by
    # a later FOSSIL verdict.
    if mode == "worktree":
        report.dirty_paths = _worktree_dirty(cwd)
        if report.dirty_paths:
            report.verdict = "DRIFT"
            shown = ", ".join(report.dirty_paths[:5])
            if len(report.dirty_paths) > 5:
                shown += f", +{len(report.dirty_paths) - 5} more"
            report.reasons.append(
                f"{len(report.dirty_paths)} path(s) differ from {preview_ref} "
                f"on disk in {cwd} ({shown}); Vite and the Python engine serve "
                f"files from disk, not from a commit object, so this can be "
                f"live with no PR gate having seen it"
            )

    merge_trusted_paths: set[str] = set()
    if report.serves_main_tree:
        if report.preview_only:
            report.reasons.append(
                f"{len(report.preview_only)} commit(s) have no single-patch "
                f"equivalent on {main_ref}, but the preview tree is IDENTICAL "
                f"to it: they landed together (several commits squashed into "
                f"one is the usual shape). Nothing preview-only is served."
            )
    else:
        merges = _merge_commits(cwd, main_ref, preview_ref)
        if len(merges) >= fossil_threshold:
            # A three-figure merge count is the divergent-lineage shape, and
            # replaying that many merges is not a cheap check. Say what it is
            # rather than spending minutes reaching the same answer.
            _promote_to_fossil(report)
            report.reasons.append(
                f"{len(merges)} merge commits on the preview and not on "
                f"{main_ref} (ceiling {fossil_threshold}): a divergent "
                f"lineage shape, left behind by a history rewrite -- "
                f"{_fossil_shape_conclusion(mode)}."
            )
            return report
        carrying, merge_trusted_paths = _resolution_carrying_merges(cwd, main_ref, merges)
        if carrying:
            described_merges = _describe(cwd, carrying)
            carrying_commits = [
                PreviewOnlyCommit(
                    sha=sha,
                    committed_at=described_merges[sha][0],
                    subject=described_merges[sha][1],
                )
                for sha in carrying
            ]
            # `_mark_superseded_commits` and `_mark_landed_commits` already
            # ran, above, over the commits `git cherry` could see -- these
            # carrying merges are invisible to `git cherry` and only exist
            # from here on, so both checks must be applied to them
            # separately. Supersession alone is not enough (r3974660152 fixed
            # that direction, r3974766502 found this one): a merge's own
            # first-parent diff includes ordinary content legitimately
            # pulled in from its SECOND parent, which is meant to persist
            # forever, not revert -- so "has the merge's ENTIRE diff gone
            # back to its pre-merge state" can never fire for a merge that
            # keeps serving that legitimate content, even after the only
            # risky part (the hand resolution itself) is fully gone.
            # `_mark_landed_commits` is the other half: it asks whether the
            # merge's touched paths, AS CURRENTLY SERVED, coexist together
            # on some single main commit right now, which is exactly what
            # "the resolution is gone and the rest already matches main"
            # looks like.
            _mark_superseded_commits(cwd, preview_ref, carrying_commits)
            _mark_landed_commits(cwd, main_ref, preview_ref, carrying_commits)
            report.preview_only.extend(carrying_commits)
            report.reasons.append(
                f"{len(carrying)} merge commit(s) carry a conflict resolution "
                f"that is on no ordinary commit, so git cherry cannot see it; "
                f"counted as preview-only"
            )

    stale = report.stale
    # FOSSIL is checked BEFORE DRIFT for a NON-authoritative (remote) ref: a
    # ref stranded by a history rewrite has hundreds or thousands of
    # "preview-only" commits that no merge can ever clear, and calling that
    # DRIFT produces a red that is permanently true and therefore permanently
    # ignored. That argument INVERTS for the authoritative worktree
    # (r3974540460): the same fossil-shaped history is code being served
    # right now, so a permanently-true red is the correct outcome, not a
    # problem to avoid -- `_promote_to_fossil` preserves DRIFT there instead.
    if len(stale) >= fossil_threshold:
        oldest = min(stale, key=lambda c: c.committed_at)
        newest = max(stale, key=lambda c: c.committed_at)
        _promote_to_fossil(report)
        report.reasons.append(
            f"{len(stale)} preview-only commits (ceiling {fossil_threshold}), "
            f"spanning {oldest.committed_at.date()} to {newest.committed_at.date()}: "
            f"a divergent lineage shape -- {_fossil_shape_conclusion(mode)}. "
            f"Nothing here is merged to main."
        )
        return report

    _apply_never_coexisted_check(cwd, main_ref, preview_ref, report, landed_shas)

    # A sixth blind spot: the fourth check above unions '-' rows against
    # themselves, `_mark_landed_commits` unions '+' rows against themselves,
    # and the merge-resolution check above unions a merge's own touched
    # paths against themselves -- three separate unions, each checked for
    # coexistence only WITHIN its own class. A preview can serve one path
    # trusted by one class and a different path trusted by another, with
    # neither class's check ever seeing the other's path, so the combination
    # can pass every class-level check while no single main commit ever held
    # it together. This is the one check that unions ACROSS all three.
    _apply_cross_class_never_coexisted_check(
        cwd, main_ref, preview_ref, report, landed_shas, merge_trusted_paths
    )

    if stale:
        report.verdict = "DRIFT"
        report.reasons.append(
            f"{len(stale)} preview-only commit(s) older than {max_age_hours}h "
            f"with no patch-equivalent on {main_ref}"
        )
    if report.behind > max_behind:
        report.verdict = "DRIFT"
        report.reasons.append(
            f"preview is {report.behind} commits behind {main_ref} "
            f"(ceiling {max_behind}); the idle fast-forward is not running"
        )
    return report
