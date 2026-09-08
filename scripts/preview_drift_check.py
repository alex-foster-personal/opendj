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
    verdict: str = "OK"
    reasons: list[str] = field(default_factory=list)

    @property
    def stale(self) -> list[PreviewOnlyCommit]:
        return [c for c in self.preview_only if c.age_hours > self.max_age_hours]

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
            "preview_only_count": len(self.preview_only),
            "preview_only_stale": [c.as_dict() for c in self.stale],
            "reasons": self.reasons,
        }


def _now() -> datetime:
    """UTC now. Never a hand-typed Z; the tzinfo does the labelling."""
    return datetime.now(UTC)


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

    stale = report.stale
    # FOSSIL is checked BEFORE DRIFT. A ref stranded by a history rewrite has
    # hundreds or thousands of "preview-only" commits that no merge can ever
    # clear, and calling that DRIFT produces a red that is permanently true and
    # therefore permanently ignored.
    if len(stale) >= fossil_threshold:
        oldest = min(stale, key=lambda c: c.committed_at)
        newest = max(stale, key=lambda c: c.committed_at)
        report.verdict = "FOSSIL"
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
            _git(cwd, "fetch", "--quiet", "origin", "main", args.branch)
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
