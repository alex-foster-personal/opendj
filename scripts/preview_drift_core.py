"""Measurement core for the preview-branch drift check (DEVOPS-05).

Pure git measurement and the ``Report``/``PreviewOnlyCommit`` model, split out
of ``scripts/preview_drift_check.py`` so neither file crosses the 600-line
file-size ratchet. The CLI module owns argument parsing, rendering, and
``main()``; everything it needs from here it imports explicitly, so
``python -m scripts.preview_drift_check`` and
``import scripts.preview_drift_check`` are unaffected by this split.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_MAX_AGE_HOURS = 24
# The Air fast-forwards every 120 s while idle. A day of merges is ~40 commits,
# so 200 is roughly five days of sync being dead: late enough to be a fault, far
# enough from normal that a busy afternoon cannot trip it.
DEFAULT_MAX_BEHIND = 200

# Above this many preview-only commits the ref is not a preview carrying quick
# fixes, it is a DIVERGENT LINEAGE. The policy says the preview holds at most a
# handful of pin fixes for at most 24 h, so any three-figure count is a
# different kind of object and needs a different fix (re-point or delete the
# ref), not "merge these to main".
#
# This is an INVARIANT of the policy, deliberately not a pinned rewrite
# timestamp. A timestamp was tried first and was wrong on the very first live
# run: `origin/chrome-loop-preview-live` carries 1615 preview-only commits and
# was still receiving merges from the PRE-REWRITE main lineage as late as
# Sun 6 Sep 2026, three days AFTER the Wed 3 Sep rewrite, so "older than the
# rewrite" classified it as ordinary drift. The count does not rot and does
# not need maintaining.
FOSSIL_THRESHOLD = 100


class MeasurementError(RuntimeError):
    """The check could not measure its subject. Never rendered as a verdict."""


# ----- git ----------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise MeasurementError(
            f"git {' '.join(args)} failed in {cwd} "
            f"(exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def fetch_origin(cwd: Path, refs: list[str]) -> None:
    """Fetch ``refs`` from origin. Raises MeasurementError on failure."""
    _git(cwd, "fetch", "--quiet", "origin", *refs)


def _rev_parse(cwd: Path, ref: str) -> str:
    """Resolve ``ref``, raising MeasurementError when it does not exist.

    ``--verify`` plus the explicit existence check is the negative control: an
    absent branch must report absent, loudly, rather than resolving to an empty
    string that later reads as "nothing to report".
    """
    out = _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    if not out:
        raise MeasurementError(f"ref does not resolve to a commit: {ref}")
    return out


def _trees_identical(cwd: Path, left: str, right: str, *paths: str) -> bool:
    """True when two refs have the SAME content, or the same content restricted
    to ``paths`` when given.

    ``git diff --quiet`` exits 0 for identical and 1 for different; anything
    else is a FAILED MEASUREMENT and must never be rendered as either answer.
    """
    proc = subprocess.run(
        ["git", "diff", "--quiet", left, right, *(["--", *paths] if paths else [])],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise MeasurementError(
        f"git diff --quiet {left} {right} failed in {cwd} "
        f"(exit {proc.returncode}): {proc.stderr.strip()}"
    )


def _diff_paths(cwd: Path, left: str, right: str) -> set[str]:
    """Paths that differ between two treeish refs."""
    return set(_git(cwd, "diff", "--name-only", left, right).splitlines())


def _paths_ever_together_on_main(
    cwd: Path, main_ref: str, treeish: str, paths: set[str]
) -> bool:
    """True when some SINGLE commit on ``main_ref`` matches ``treeish`` at
    EVERY one of ``paths`` SIMULTANEOUSLY.

    Checking each path's presence on main independently is not this test:
    main can hold each path's matching content at a different, non-
    overlapping commit and never once hold them all together, so the
    preview serves multi-file state no main commit ever contained. This is
    the ONE notion of containment shared by both call sites (a merge
    resolution's touched paths, and a preview-only commit's own touched
    paths) -- tree containment at a set of paths, searched across main's
    full HISTORY rather than its current tip, so a resolution main landed
    and later edited further still counts.

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


def _worktree_dirty(cwd: Path) -> list[str]:
    """Tracked/unstaged/untracked paths that differ from HEAD on disk.

    ``HEAD`` is a commit object; it says nothing about a staged, unstaged, or
    untracked edit sitting on top of it. Vite and the Python engine both
    serve files straight off disk in ``cwd``, not from a commit, so this is
    the only thing that answers what a browser hitting the preview actually
    gets.
    """
    out = _git(cwd, "status", "--porcelain", "--untracked-files=all")
    return [line[3:] for line in out.splitlines() if line.strip()]


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
            if touched and _paths_ever_together_on_main(
                cwd, main_ref, merge_tree, touched
            ):
                continue
        else:
            if main_trees is None:
                main_trees = set(_git(cwd, "log", main_ref, "--format=%T").split())
            if merge_tree in main_trees:
                continue
        carrying.append(sha)
    return carrying


def _describe(cwd: Path, shas: list[str]) -> dict[str, tuple[datetime, str]]:
    """Committer date and subject for every sha, in ONE git call.

    A stranded pre-rewrite ref carries thousands of commits (1615 on
    Tue 8 Sep 2026). Two subprocesses each would be over three thousand
    process spawns and takes minutes; one ``git log --stdin --no-walk`` takes
    milliseconds. The count is data, not an exception, so the check must stay
    cheap at that size or it will simply be turned off.
    """
    if not shas:
        return {}
    proc = subprocess.run(
        ["git", "log", "--no-walk", "--stdin", "--format=%H%x00%cI%x00%s"],
        cwd=cwd,
        input="\n".join(shas) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise MeasurementError(
            f"git log --no-walk failed in {cwd} "
            f"(exit {proc.returncode}): {proc.stderr.strip()}"
        )
    described: dict[str, tuple[datetime, str]] = {}
    for line in proc.stdout.splitlines():
        if not line:
            continue
        sha, raw_date, subject = line.split("\0", 2)
        described[sha] = (
            datetime.fromisoformat(raw_date).astimezone(UTC),
            subject,
        )
    missing = [s for s in shas if s not in described]
    if missing:
        # Presence, not absence: every sha asked for must come back, or the
        # measurement is partial and must not be rendered as a verdict.
        raise MeasurementError(
            f"git log returned {len(described)} of {len(shas)} commits; "
            f"first missing: {missing[0]}"
        )
    return described


# ----- model --------------------------------------------------------------


@dataclass
class PreviewOnlyCommit:
    sha: str
    committed_at: datetime
    subject: str
    superseded: bool = False
    landed: bool = False

    @property
    def age_hours(self) -> float:
        return (_now() - self.committed_at).total_seconds() / 3600.0

    def as_dict(self) -> dict[str, object]:
        return {
            "sha": self.sha,
            "committed_at": self.committed_at.isoformat(),
            "age_hours": round(self.age_hours, 2),
            "subject": self.subject,
        }


@dataclass
class Report:
    mode: str
    preview_ref: str
    main_ref: str
    preview_sha: str = ""
    main_sha: str = ""
    behind: int = 0
    max_behind: int = DEFAULT_MAX_BEHIND
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS
    preview_only: list[PreviewOnlyCommit] = field(default_factory=list)
    serves_main_tree: bool = False
    dirty_paths: list[str] = field(default_factory=list)
    verdict: str = "OK"
    reasons: list[str] = field(default_factory=list)

    @property
    def stale(self) -> list[PreviewOnlyCommit]:
        """The preview-only commits old enough to be a finding.

        Empty whenever the preview TREE is identical to main's: patch-id
        comparison matches one patch at a time, so a topic of several commits
        that main landed as one squash is reported as several unmatched
        commits even though the served tree is exactly main's. Nothing
        preview-only is being served, so there is nothing for a human to
        merge. A commit marked ``superseded`` is excluded for the same
        reason at finer grain: its own later history already undid it (an
        explicit revert, or any edit that happens to land back there), so
        nothing of ITS content is being served either, even though the tips
        as a whole still differ. A commit marked ``landed`` is excluded for a
        third reason at the same finer grain: main took the squash AND SOME
        UNRELATED commit afterward, so the tips no longer match even though
        every path this commit touched reached main together in one tree.
        ``preview_only`` keeps the raw rows either way.
        """
        if self.serves_main_tree:
            return []
        return [
            c
            for c in self.preview_only
            if c.age_hours > self.max_age_hours and not c.superseded and not c.landed
        ]

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "verdict": self.verdict,
            "preview_ref": self.preview_ref,
            "preview_sha": self.preview_sha,
            "main_ref": self.main_ref,
            "main_sha": self.main_sha,
            "behind": self.behind,
            "max_behind": self.max_behind,
            "max_age_hours": self.max_age_hours,
            "serves_main_tree": self.serves_main_tree,
            "dirty_paths": self.dirty_paths,
            "preview_only_count": len(self.preview_only),
            "preview_only_stale": [c.as_dict() for c in self.stale],
            "reasons": self.reasons,
        }


def _now() -> datetime:
    """UTC now. Never a hand-typed Z; the tzinfo does the labelling."""
    return datetime.now(UTC)


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


# ----- the check ------------------------------------------------------------


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
    if not report.serves_main_tree:
        _mark_superseded_commits(cwd, preview_ref, report.preview_only)
        _mark_landed_commits(cwd, main_ref, preview_ref, report.preview_only)

    # A fourth blind spot neither commit nor tree comparison can see: in
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
