"""Git plumbing and the measurement model for the preview-branch drift check
(DEVOPS-05).

Split out of ``scripts/preview_drift_core.py`` (which itself split out of
``scripts/preview_drift_check.py``) so no file in the set crosses the
600-line file-size ratchet: this is the LEAF layer (raw ``git`` calls, the
``Report``/``PreviewOnlyCommit`` model), with no notion of "containment" or
"drift" -- ``scripts/preview_drift_containment.py`` builds the containment
tests on top of it, and ``scripts/preview_drift_core.py`` builds ``evaluate()``
on top of both. One direction only: this module imports nothing from either
of the other two.
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
    """Paths that differ between two treeish refs.

    ``--no-renames`` is required: rename detection is ON by default for
    ``git diff``, and a detected rename reports ONLY the destination path in
    ``--name-only`` output, dropping the source entirely (r3974399470). A
    caller unioning this into a containment check would then only ever
    verify the renamed-TO path against main, silently never checking
    whether main still holds the renamed-FROM path unchanged -- exactly the
    kind of divergence this whole module exists to catch. Disabling rename
    detection reports a rename as a plain delete-plus-add, both paths
    included, at the cost of nothing this module ever wanted from rename
    detection in the first place (it never inspects diff CONTENT, only the
    set of touched paths).
    """
    return set(_git(cwd, "diff", "--no-renames", "--name-only", left, right).splitlines())


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


def _now() -> datetime:
    """UTC now. Never a hand-typed Z; the tzinfo does the labelling."""
    return datetime.now(UTC)


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
