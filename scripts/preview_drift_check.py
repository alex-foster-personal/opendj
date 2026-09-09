"""Preview-branch drift check (DEVOPS-05).

The live review preview (branch ``chrome-loop-preview-live``, worktree
``/Users/dev/Music/music-dj-tools-wt-p0-audio`` on the Air, engine :8728,
vite :9448) must never serve code that no PR gate has seen. Policy:
``docs/ops/periodic-checks.md``.

Two failure shapes this reports:

``BEHIND``
    The preview is more than ``--max-behind`` commits behind ``main``. The Air's
    ``com.af.opendj-preview-watch`` fast-forwards every 120 s while idle, so a
    large gap means the sync is broken, not merely late.

``DRIFT``
    A commit exists only on the preview and is older than ``--max-age-hours``.
    Detected with ``git cherry``, which compares PATCH IDS, so a commit that
    landed on main via squash or rebase counts as landed and does not show up.

    ``git cherry`` is wrong in BOTH directions on its own, so two tree-level
    measurements bound it. It compares patches ONE AT A TIME, so a preview
    topic of several commits that main landed as a SINGLE squash comes back as
    several unmatched commits; when the preview tree is IDENTICAL to main's,
    nothing preview-only is being served and those rows cannot be drift. And it
    skips MERGE COMMITS, so a merge whose conflict resolution changed the
    result carries content no ordinary commit records and no ``+`` row can
    show; every merge on the preview side is replayed with
    ``git merge-tree --write-tree``, and one whose recorded tree is not what
    merging its parents produces is counted as preview-only. A trivially
    resolvable merge reproduces its tree exactly, so an ordinary "merge main
    into preview" is not flagged.

And one shape that is deliberately NOT either of those:

``FOSSIL``
    So many preview-only commits that the ref cannot be a preview carrying
    quick fixes: it is a divergent lineage left behind by a history rewrite.
    That needs the ref re-pointed or deleted, not merged, so reporting it as
    DRIFT would be a red no amount of merging can clear. Measured Tue 8 Sep
    2026: ``origin/chrome-loop-preview-live`` sat 1615 commits "ahead" and 401
    behind, still taking merges from the PRE-REWRITE main lineage as late as
    Sun 6 Sep 2026, while the LIVE worktree head on the Air was fully
    contained in main.

That distinction is the whole reason for the two modes:

``--worktree PATH``
    Authoritative. Reads the head actually being served. Runs on the Air.

``--remote``
    Reads ``origin/<preview branch>`` only. Runs anywhere with a clone, which
    is what makes it usable from a nucbox timer. It can see a fossil ref and a
    branch that was pushed and never merged; it CANNOT see the live preview
    head, and it says so in its own output rather than implying it measured
    something it did not.

Exit codes:

    0   OK        no drift, not behind
    1   DRIFT     or BEHIND: something needs a human
    2   FOSSIL    the ref measured is pre-rewrite; nothing to merge, re-point it
    3   UNKNOWN   the check could not measure (missing ref, git failure)

UNKNOWN is a distinct code on purpose. A tool that cannot measure must report
UNKNOWN, never a verdict (``.claude/rules/verification.md``): an absent branch
returning "no drift" is the failure mode this whole file exists to avoid.

Usage::

    python -m scripts.preview_drift_check --worktree /path/to/preview
    python -m scripts.preview_drift_check --remote
    python -m scripts.preview_drift_check --remote --json
"""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

DEFAULT_PREVIEW_BRANCH = "chrome-loop-preview-live"
DEFAULT_MAIN_REF = "origin/main"
DEFAULT_MAX_AGE_HOURS = 24
# The Air fast-forwards every 120 s while idle. A day of merges is ~40 commits,
# so 200 is roughly five days of sync being dead: late enough to be a fault, far
# enough from normal that a busy afternoon cannot trip it.
DEFAULT_MAX_BEHIND = 200

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_FOSSIL = 2
EXIT_UNKNOWN = 3

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


_NULL_BLOB = "0" * 40


def _blob_at(cwd: Path, tree: str, path: str) -> str:
    """The blob sha ``path`` has in ``tree``, or the all-zero null sha when
    ``path`` does not exist there at all.

    A merge resolution can DELETE a path that existed in both parents, so
    ``path`` is a real member of ``touched`` with nothing to resolve at
    ``merge_tree``. ``rev-parse`` on an absent path is a plain miss, not a
    broken measurement (``--verify --quiet`` exits 1 for it, never anything
    else), so it must not raise. The all-zero sha is git's own convention
    for "no blob" in ``--raw`` diff output, reused here so a deletion
    recorded by ``_blob_ever_on`` below compares equal to one recorded here.
    """
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{tree}:{path}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return proc.stdout.strip()
    if proc.returncode == 1:
        return _NULL_BLOB
    raise MeasurementError(
        f"git rev-parse --verify --quiet {tree}:{path} failed in {cwd} "
        f"(exit {proc.returncode}): {proc.stderr.strip()}"
    )


def _blob_ever_on(cwd: Path, ref: str, path: str) -> set[str]:
    """Every blob sha ``path`` has EVER had on ``ref``, across its full history.

    A tip-only comparison misses a resolution main landed and later edited
    further: the tip content has moved on, but the resolved content still
    reached main at some point. ``--raw`` on a path-scoped log prints the
    post-image blob sha for every commit that touched the path, in one call
    regardless of history length.
    """
    out = _git(cwd, "log", ref, "--format=", "--raw", "--no-abbrev", "--", path)
    shas: set[str] = set()
    for line in out.splitlines():
        if not line.startswith(":"):
            continue
        fields = line.split()
        if len(fields) >= 4:
            shas.add(fields[3])
    return shas


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

    "Against main" means main's whole HISTORY at each touched path, not its
    current tip: if main lands the resolution and later edits that same path
    again, the tip no longer matches, but the resolved content still reached
    main at some point and must not re-age into a false DRIFT. ``git log
    --raw`` on the path enumerates every blob that path has ever had on main
    in one call, so this stays a single query per path regardless of how much
    main has moved since.
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
            if touched and all(
                _blob_at(cwd, merge_tree, p) in _blob_ever_on(cwd, main_ref, p)
                for p in touched
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
        as a whole still differ. ``preview_only`` keeps the raw rows either
        way.
        """
        if self.serves_main_tree:
            return []
        return [
            c
            for c in self.preview_only
            if c.age_hours > self.max_age_hours and not c.superseded
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


# ----- the check ----------------------------------------------------------


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
    if not report.serves_main_tree:
        _mark_superseded_commits(cwd, preview_ref, report.preview_only)

    # A third blind spot neither commit nor tree comparison can see: in
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


# ----- rendering ----------------------------------------------------------


def render(report: Report) -> str:
    lines = [
        f"[{report.verdict}] preview drift check ({report.mode} mode)",
        f"  preview {report.preview_ref} = {report.preview_sha[:9]}",
        f"  main    {report.main_ref} = {report.main_sha[:9]}",
        f"  behind  {report.behind} (ceiling {report.max_behind})",
        f"  preview-only commits: {len(report.preview_only)} "
        f"({len(report.stale)} older than {report.max_age_hours}h)",
    ]
    lines.extend(
        f"    + {commit.sha[:9]} {commit.age_hours:7.1f}h  {commit.subject[:70]}"
        for commit in report.stale[:20]
    )
    if len(report.stale) > 20:
        lines.append(f"    ... and {len(report.stale) - 20} more")
    lines.extend(f"  reason: {reason}" for reason in report.reasons)
    if report.mode == "remote":
        lines.append(
            "  NOTE: remote mode reads origin's ref only. It has NOT measured "
            "the live preview head on the Air; run --worktree there for that."
        )
    return "\n".join(lines)


VERDICT_EXIT = {"OK": EXIT_OK, "DRIFT": EXIT_DRIFT, "FOSSIL": EXIT_FOSSIL}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--worktree",
        type=Path,
        help="path to the live preview worktree (authoritative; run on the Air)",
    )
    target.add_argument(
        "--remote",
        action="store_true",
        help="measure origin's preview ref from any clone (nucbox timer)",
    )
    parser.add_argument("--branch", default=DEFAULT_PREVIEW_BRANCH)
    parser.add_argument("--main-ref", default=DEFAULT_MAIN_REF)
    parser.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--max-behind", type=int, default=DEFAULT_MAX_BEHIND)
    parser.add_argument("--fossil-threshold", type=int, default=FOSSIL_THRESHOLD)
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="git fetch origin before measuring (remote mode wants this)",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO_ROOT,
        help="clone to read origin refs from, in remote mode",
    )
    args = parser.parse_args(argv)

    if args.remote:
        cwd, preview_ref, mode = args.repo, f"origin/{args.branch}", "remote"
    else:
        cwd, preview_ref, mode = args.worktree, "HEAD", "worktree"

    # Outside the try: a missing directory is a caller error, and wrapping it
    # in the same handler as a git failure buys nothing but a TRY301.
    if not cwd.is_dir():
        print(f"[UNKNOWN] preview drift check could not measure: not a directory: {cwd}")
        return EXIT_UNKNOWN

    try:
        if args.fetch:
            # Worktree mode reads the live preview head from HEAD, not from
            # origin's preview ref, so it only needs `main` current. Remote
            # mode reads BOTH from origin. Fetching the preview branch in
            # worktree mode too means a deleted remote preview ref (the
            # remediation this checker itself recommends for a FOSSIL) fails
            # the whole fetch before the live worktree is ever inspected.
            refs = ["main", args.branch] if args.remote else ["main"]
            _git(cwd, "fetch", "--quiet", "origin", *refs)
        report = evaluate(
            cwd=cwd,
            preview_ref=preview_ref,
            main_ref=args.main_ref,
            mode=mode,
            max_age_hours=args.max_age_hours,
            max_behind=args.max_behind,
            fossil_threshold=args.fossil_threshold,
        )
    except MeasurementError as exc:
        payload = {"mode": mode, "verdict": "UNKNOWN", "error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"[UNKNOWN] preview drift check could not measure: {exc}")
        return EXIT_UNKNOWN

    print(json.dumps(report.as_dict(), indent=2) if args.json else render(report))
    return VERDICT_EXIT[report.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
