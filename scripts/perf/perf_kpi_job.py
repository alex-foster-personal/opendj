"""Perf KPI job unit: nightly deck-load capture and live engine health probe.

Requirements (issue #1506):
- [if] warm anlz median exceeds 3x the trailing 7-day ledger median [then] exit non-zero
  and record a ceiling breach [else broken].
- [if] two consecutive live health probes time out [then] restart the preview engine once
  [else broken].
- [if] a probe cannot measure [then] append an error ledger row, never a number [else broken].

-Codex
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from scripts.perf.kpi_ledger_append import append_entries, load_ledger
from scripts.perf.perf_kpi_config import (
    LEDGER_PR_BRANCH,
    LEDGER_PR_TITLE,
    REPO_ROOT,
    load_config,
    require_machine_label,
)
from scripts.perf.perf_kpi_health import HealthConfig, build_restart_command, run_health_tick
from scripts.perf.perf_kpi_nightly import (
    acquire_nightly_engine,
    default_probe,
    run_nightly,
    stop_scratch_engine,
)

REPOSITORY = "maintainer/music-dj-tools"


def _git_sha(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    return completed.stdout.strip()


def _engine_log_path() -> Path:
    return (
        Path.home() / "Library/Application Support/com.opendj.desktop.chrome-loop/logs/engine.log"
    )


def cmd_health(config) -> int:
    restart = build_restart_command(config.preview_engine_label)
    return run_health_tick(
        HealthConfig(
            health_url=config.preview_health_url,
            state_file=config.health_state,
            health_log=config.health_log,
            timeout_s=5.0,
            failure_threshold=2,
            restart_command=restart,
            engine_log_path=_engine_log_path(),
        )
    )


def cmd_nightly(config, *, base_url: str | None, skip_pr: bool) -> int:
    require_machine_label(config)
    url, proc, _log_path, prep_code = acquire_nightly_engine(config, base_url=base_url)
    if prep_code != 0:
        return prep_code
    try:
        outcome = run_nightly(
            config,
            base_url=url,
            git_sha=_git_sha(REPO_ROOT),
            probe=default_probe,
            file_issue=not skip_pr,
        )
        if not skip_pr:
            update_ledger_pr(REPO_ROOT, config.ledger_path, config.ledger_worktree)
        return outcome.exit_code
    finally:
        if proc is not None:
            stop_scratch_engine(proc)


def _remove_worktree_if_present(repo_root: Path, worktree_dir: Path) -> None:
    """Clean up a leftover ledger worktree from a prior run.

    Fails loud on a real removal error instead of discarding it
    (claude-review, PR #3827, round 2, P2): round 1's fix still ran ``git
    worktree remove`` with ``check=False`` and then rmtree'd the directory
    unconditionally, so a locked worktree, a wrong-repo path, or a
    permissions error was thrown away exactly as before -- only the
    docstring changed. This now raises with git's own stderr for anything
    other than "not a working tree" (the ordinary case: nothing was ever
    registered there). ``git worktree prune`` now always runs first
    (claude-review, round 2, P3): the old early return on a missing
    directory meant a state dir wiped out from under git, or a directory
    removed after a crash, left the registration behind for the next
    ``git worktree add`` to trip over -- exactly the failure this function
    exists to prevent.
    """
    subprocess.run(["git", "worktree", "prune"], check=True, cwd=repo_root, capture_output=True)
    if not worktree_dir.exists():
        return
    removed = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_dir)],
        check=False,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if removed.returncode == 0:
        return
    if "is not a working tree" not in removed.stderr:
        raise RuntimeError(f"git worktree remove {worktree_dir} failed: {removed.stderr.strip()}")
    shutil.rmtree(worktree_dir)
    subprocess.run(["git", "worktree", "prune"], check=True, cwd=repo_root, capture_output=True)


def _new_entries_since(base_entries: list, local_entries: list) -> list:
    """Entries in ``local_entries`` whose exact content isn't already in ``base_entries``."""
    seen = {json.dumps(entry, sort_keys=True) for entry in base_entries}
    return [entry for entry in local_entries if json.dumps(entry, sort_keys=True) not in seen]


def _ledger_entries_at_ref(repo_root: Path, ref: str) -> list:
    """``docs/perf/kpi-ledger.json``'s entries at ``ref``, or ``[]`` if the
    path genuinely doesn't exist there.

    The ``f"{ref}:docs/perf/kpi-ledger.json"`` argument IS a Python
    f-string interpolation, deliberately -- there is just no SHELL involved
    in building it, since it is one argv element passed straight to
    ``subprocess.run`` (never ``shell=True``), so there is nothing to
    mis-parse the ``:`` separator the way an unbraced zsh ``$SHA:path``
    would (corrected wording, claude-review, PR #3827, round 4, P2).

    Only "path does not exist at this ref" returns ``[]`` (claude-review,
    round 4, P2): a bad ref, a corrupt object, or a permissions error used
    to return the same empty list, silently falling back to branch-only
    entries and risking a re-published duplicate with no error. ``main`` is
    always freshly fetched immediately before this is called, so any other
    failure here is a real problem, not an expected absence.
    """
    completed = subprocess.run(
        ["git", "show", f"{ref}:docs/perf/kpi-ledger.json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        if "does not exist in" in completed.stderr:
            return []
        raise RuntimeError(
            f"git show {ref}:docs/perf/kpi-ledger.json failed: {completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)["entries"]


def update_ledger_pr(
    repo_root: Path,
    ledger_path: Path,
    worktree_dir: Path,
) -> None:
    """Open or update the standing docs PR for nightly ledger appends.

    Runs entirely inside a DEDICATED worktree, never ``repo_root`` itself
    (Codex review, PR #3827): the nightly launchd job installed on silver
    shares ``repo_root`` with whatever checkout lives there, and the old
    ``git checkout -B <branch> origin/main`` run directly in it would switch
    that checkout's branch out from under any in-progress work the moment
    the job fires at 04:00.

    MERGES the ledger rather than overwriting it (claude-review, PR #3827,
    P1/BLOCKING): silver and Air both publish through this one standing
    branch. The old code replaced the branch's (or origin/main's)
    ``kpi-ledger.json`` outright with a copy of REPO_ROOT's local file, so
    whichever host ran second -- or ran against a REPO_ROOT that hadn't
    pulled the other host's already-merged entries -- silently deleted them.
    This instead diffs the LOCAL ledger's entries against the branch's (or
    origin/main's) current entries by exact content and appends only what's
    genuinely new, through the same byte-preserving ``append_entries`` the
    nightly capture itself uses, so a concurrent writer's history is never
    at risk.

    Also UPDATES an already-open ledger PR instead of skipping it
    (claude-review, PR #3827, P2): the old code returned as soon as ``gh pr
    list`` found one open, so whichever host's nightly run found a PR
    already open (from the other host, or from its own prior night) never
    published -- its entries sat only in that host's REPO_ROOT until the PR
    merged and a later run happened to find none open. This checks out the
    EXISTING branch instead of origin/main when one is open, so every
    night's new entries reach it either way.

    ``worktree_dir`` has no default (claude-review, PR #3827, round 2, P3):
    round 1 kept ``LEDGER_WORKTREE_DIR`` (pinned to ``DEFAULT_STATE_DIR`` at
    import time) as a fallback default, so a future caller that left the
    argument out would silently get the shared path again, and two installs
    with different state dirs could still force-remove each other's
    in-flight worktree. Every real caller must pass ``config.ledger_worktree``.
    """
    completed = subprocess.run(
        ["gh", "pr", "list", "--repo", REPOSITORY, "--head", LEDGER_PR_BRANCH, "--json", "number"],
        check=True,
        capture_output=True,
        text=True,
    )
    # A failed `gh pr list` (claude-review, PR #3827, round 2, P3) used to
    # read identically to "no PR open": the job would then build from
    # origin/main, and either the push got a non-fast-forward rejection
    # against the branch a PR already existed for, or `gh pr create` failed
    # for the same auth/network reason -- both far from the real cause.
    # `check=True` now raises with gh's own stderr immediately instead.
    pr_already_open = completed.stdout.strip() not in ("", "[]")
    branch = LEDGER_PR_BRANCH
    subprocess.run(["git", "fetch", "origin", "main"], check=True, cwd=repo_root)
    base_ref = "origin/main"
    if pr_already_open:
        subprocess.run(["git", "fetch", "origin", branch], check=True, cwd=repo_root)
        base_ref = f"origin/{branch}"
    # The push lease is taken HERE, right after the fetch(es) above, not
    # from a fresh `git ls-remote` immediately before the push (claude-review,
    # PR #3827, round 5, P1/BLOCKING): a lease re-checked right before
    # pushing always matches whatever the remote currently is, which makes
    # the "force-with-lease" push an unconditional force push -- exactly
    # the concurrent-overwrite it was meant to prevent. The lease must
    # reflect the ref this run's commit was actually BUILT on top of, so a
    # push from the other host landing in between is detected and rejected.
    lease_ls_remote = subprocess.run(
        ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_root,
    ).stdout.split()
    lease_value = lease_ls_remote[0] if lease_ls_remote else ""
    _remove_worktree_if_present(repo_root, worktree_dir)
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    # --detach, never `-B <branch>` (claude-review, PR #3827, round 5, P2):
    # a worktree can't reset a branch another worktree already has checked
    # out. Any host where an OLDER version of this job got past the
    # PR-open check and left REPO_ROOT itself sitting on
    # perf/kpi-nightly-ledger would fail this line on every subsequent
    # run. A detached worktree never contends for the branch name; the
    # push below names the branch explicitly instead.
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree_dir), base_ref],
        check=True,
        cwd=repo_root,
    )
    try:
        dest = worktree_dir / "docs" / "perf" / "kpi-ledger.json"
        local_entries = load_ledger(ledger_path)["entries"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Dedup against the UNION of the branch's entries and origin/main's
        # (claude-review, PR #3827, round 3, P2), not the branch alone: an
        # entry can reach main by another route after this branch was cut
        # (another host's ledger PR already merged) while this host's
        # REPO_ROOT has since pulled main and holds that entry locally too.
        # Comparing only against the branch would re-append it, producing a
        # duplicate once this branch also merges.
        branch_entries = load_ledger(dest)["entries"] if dest.exists() else []
        main_entries = _ledger_entries_at_ref(repo_root, "origin/main")
        base_entries = branch_entries + [
            entry
            for entry in main_entries
            if json.dumps(entry, sort_keys=True)
            not in {json.dumps(e, sort_keys=True) for e in branch_entries}
        ]
        new_entries = _new_entries_since(base_entries, local_entries)
        if not new_entries:
            return
        append_entries(dest, new_entries, validate=False)
        subprocess.run(["git", "add", "docs/perf/kpi-ledger.json"], check=True, cwd=worktree_dir)
        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                f"perf(kpi): nightly ledger append ({len(new_entries)} new)\n\n-Codex",
            ],
            check=True,
            cwd=worktree_dir,
        )
        # --force-with-lease against the SHA fetched at the top of this
        # function (claude-review, PR #3827, round 5, P1/BLOCKING -- round
        # 4's version re-checked the lease with a fresh `git ls-remote`
        # immediately before this push, which always matches the current
        # remote tip and so was an unconditional force push in disguise: a
        # concurrent push from the other host landing between that
        # ls-remote and this one would have been silently overwritten
        # instead of rejected). `lease_value` is the branch's SHA as of
        # the fetch this run's commit was actually built on top of, so a
        # push that landed after that point is detected here. The worktree
        # is DETACHED (round 5, P2), so there is no local branch to push
        # from by name -- push HEAD to the branch ref explicitly.
        subprocess.run(
            [
                "git",
                "push",
                f"--force-with-lease={branch}:{lease_value}",
                "origin",
                f"HEAD:refs/heads/{branch}",
            ],
            check=True,
            cwd=worktree_dir,
        )
    finally:
        _remove_worktree_if_present(repo_root, worktree_dir)
    if not pr_already_open:
        subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                REPOSITORY,
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                LEDGER_PR_TITLE,
                "--body",
                "Standing docs PR for nightly perf KPI ledger appends. Never merges by itself.",
            ],
            check=True,
            cwd=repo_root,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("nightly", "health"))
    parser.add_argument("--base-url", help="throwaway engine base URL for nightly mode")
    parser.add_argument("--skip-pr", action="store_true", help="do not open/update ledger PR")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    if args.mode == "health":
        return cmd_health(config)
    return cmd_nightly(config, base_url=args.base_url, skip_pr=args.skip_pr)


if __name__ == "__main__":
    raise SystemExit(main())
