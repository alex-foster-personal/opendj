"""Preview-branch drift check (DEVOPS-05).

The live review preview (branch ``chrome-loop-preview-live``, worktree
``/Users/dev/code/music-dj-tools-wt-p0-audio`` on the Air, engine :8728,
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
    ``--remote`` mode only: so many preview-only commits that the ref cannot
    be a preview carrying quick fixes, it is a divergent lineage left behind
    by a history rewrite. That needs the ref re-pointed or deleted, not
    merged, so reporting it as DRIFT would be a red no amount of merging can
    clear. Measured Tue 8 Sep 2026: ``origin/chrome-loop-preview-live`` sat
    1615 commits "ahead" and 401 behind, still taking merges from the
    PRE-REWRITE main lineage as late as Sun 6 Sep 2026, while the LIVE
    worktree head on the Air was fully contained in main. ``--worktree`` mode
    never reports FOSSIL, even past the same threshold: ``cwd`` there IS the
    served directory, so a fossil-shaped history at HEAD is still live,
    ungated code being served right now, and DRIFT is preserved instead
    (r3974540460).

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

The measurement core (git plumbing, the ``Report``/``PreviewOnlyCommit``
model, ``evaluate()``) lives in ``scripts/preview_drift_core.py``, split out
so neither file crosses the 600-line file-size ratchet. This module owns
argument parsing, rendering, and ``main()``.

Usage::

    python -m scripts.preview_drift_check --worktree /path/to/preview
    python -m scripts.preview_drift_check --remote
    python -m scripts.preview_drift_check --remote --json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.preview_drift_core import (
    DEFAULT_MAX_AGE_HOURS,
    DEFAULT_MAX_BEHIND,
    FOSSIL_THRESHOLD,
    MeasurementError,
    Report,
    evaluate,
    fetch_origin,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

DEFAULT_PREVIEW_BRANCH = "chrome-loop-preview-live"
DEFAULT_MAIN_REF = "origin/main"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_FOSSIL = 2
EXIT_UNKNOWN = 3


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
            fetch_origin(cwd, refs)
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
