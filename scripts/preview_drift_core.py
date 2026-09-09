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
    _mark_landed_commits,
    _mark_superseded_commits,
    _merge_commits,
    _patch_equivalent_never_coexisted,
    _resolution_carrying_merges,
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


def _promote_to_fossil(report: Report, dirty_forces_drift: bool) -> None:
    """Escalate ``report.verdict`` to FOSSIL, unless a dirty worktree already
    forced DRIFT. A currently-served, uncommitted edit is a more urgent,
    orthogonal finding than a stranded fossil ref -- FOSSIL's own message
    ("safe to re-point or delete, not unmerged work") is the wrong thing to
    say the instant something live and ungated is on disk, so it must never
    mask the DRIFT the dirty check already raised.
    """
    if not dirty_forces_drift:
        report.verdict = "FOSSIL"


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
    # equals main and every check above reports clean. Set before the
    # FOSSIL/DRIFT branches below, and `dirty_forces_drift` keeps this DRIFT
    # from being overwritten by a later FOSSIL verdict: FOSSIL means "stale
    # rewritten lineage, safe to ignore as unmerged work", which is the wrong
    # message the instant something currently being served is uncommitted --
    # that is live and ungated regardless of how the ref's history looks.
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
    dirty_forces_drift = bool(report.dirty_paths)

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
            _promote_to_fossil(report, dirty_forces_drift)
            report.reasons.append(
                f"{len(merges)} merge commits on the preview and not on "
                f"{main_ref} (ceiling {fossil_threshold}). That is a divergent "
                f"lineage left behind by a history rewrite, not unmerged "
                f"preview work: re-point or delete the ref."
            )
            return report
        carrying = _resolution_carrying_merges(cwd, main_ref, merges)
        if carrying:
            described_merges = _describe(cwd, carrying)
            report.preview_only.extend(
                PreviewOnlyCommit(
                    sha=sha,
                    committed_at=described_merges[sha][0],
                    subject=described_merges[sha][1],
                )
                for sha in carrying
            )
            report.reasons.append(
                f"{len(carrying)} merge commit(s) carry a conflict resolution "
                f"that is on no ordinary commit, so git cherry cannot see it; "
                f"counted as preview-only"
            )

    stale = report.stale
    # FOSSIL is checked BEFORE DRIFT. A ref stranded by a history rewrite has
    # hundreds or thousands of "preview-only" commits that no merge can ever
    # clear, and calling that DRIFT produces a red that is permanently true and
    # therefore permanently ignored.
    if len(stale) >= fossil_threshold:
        oldest = min(stale, key=lambda c: c.committed_at)
        newest = max(stale, key=lambda c: c.committed_at)
        _promote_to_fossil(report, dirty_forces_drift)
        report.reasons.append(
            f"{len(stale)} preview-only commits (ceiling {fossil_threshold}), "
            f"spanning {oldest.committed_at.date()} to {newest.committed_at.date()}. "
            f"That is a divergent lineage left behind by a history rewrite, not "
            f"unmerged preview work: re-point or delete the ref. Nothing here is "
            f"merged to main."
        )
        return report

    _apply_never_coexisted_check(cwd, main_ref, preview_ref, report, landed_shas)

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
