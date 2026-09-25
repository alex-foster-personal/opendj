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
from datetime import UTC, datetime
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


def _restore_tracked_ledger(
    repo_root: Path,
    ledger_path: Path,
    *,
    outbox_dir: Path,
    now: datetime | None = None,
) -> None:
    """Undo `run_nightly`'s direct write to a ledger tracked inside `repo_root`.

    BLOCKING, found by review on PR #3827 (Codex): with the default
    ``MDT_PERF_KPI_LEDGER``, `run_nightly` appends this run's rows straight
    into the CHECKED-OUT, git-tracked ``docs/perf/kpi-ledger.json`` -- the
    same file ``scripts/autoreposync.sh:852-862`` inspects before pulling.
    This function has already read those rows (into ``local_entries``) and
    either published them onto the standing branch or given up trying;
    leaving that local modification in place afterward serves no purpose
    except to leave `repo_root` permanently dirty. A dirty checkout is
    exactly what `autoreposync.sh` skips, so the FIRST scheduled run could
    silently stop this install from ever receiving another `git pull`
    again. Restored unconditionally -- success or failure -- so a publish
    attempt never leaves a side effect on `repo_root` beyond what it
    actually achieved (git history, or nothing).

    Snapshots the pre-checkout content to ``outbox_dir`` FIRST whenever it
    differs from ``HEAD`` (Sol, PR #3827, P1/BLOCKING, review 5320608598):
    the old version threw that content away unconditionally, so a night
    whose publish failed lost its only copy of the measurement, and any
    unrelated uncommitted edit already sitting in ``ledger_path`` before
    this job ever ran was destroyed the same way. Neither case is this
    function's to judge -- it does not know whether the content it is
    about to discard was ever safely persisted elsewhere -- so it always
    keeps a durable, timestamped copy outside `repo_root`'s working tree
    (never inside it, so `repo_root` still ends up exactly as clean as
    before) rather than silently losing data on a failure path.

    A no-op when ``ledger_path`` does not live inside `repo_root`'s working
    tree at all (an env override pointing somewhere else, as several tests
    do): nothing was written to `repo_root`'s own checkout to begin with.
    """
    try:
        relative = ledger_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return
    if ledger_path.exists():
        on_disk = ledger_path.read_text(encoding="utf-8")
        committed = subprocess.run(
            ["git", "show", f"HEAD:{relative.as_posix()}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        committed_text = committed.stdout if committed.returncode == 0 else None
        if on_disk != committed_text:
            outbox_dir.mkdir(parents=True, exist_ok=True)
            stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S.%fZ")
            (outbox_dir / f"unpublished-ledger-{stamp}.json").write_text(
                on_disk, encoding="utf-8"
            )
    subprocess.run(
        ["git", "checkout", "--", str(relative)],
        check=True,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )


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

    Restores REPO_ROOT's own tracked ledger afterward, success or failure
    (Codex, PR #3827, P1/BLOCKING), preserving anything unpublished to an
    outbox first (Sol, PR #3827, P1/BLOCKING): see ``_restore_tracked_ledger``.
    """
    try:
        _update_ledger_pr_inner(repo_root, ledger_path, worktree_dir)
    finally:
        _restore_tracked_ledger(
            repo_root, ledger_path, outbox_dir=worktree_dir.parent / "unpublished-ledger-outbox"
        )


def _pr_list_argv(repository: str, branch: str) -> list[str]:
    return ["gh", "pr", "list", "--repo", repository, "--head", branch, "--json", "number"]


def _parse_pr_list_result(stdout: str) -> bool:
    """Does ``stdout`` (a `gh pr list --json number` response) show an open PR?

    Pulled out of `_pr_already_open` as its own pure function (Codex, PR
    #3827, P1/BLOCKING, review comment 4106092917) so the interesting part
    -- parsing GitHub's real response shape -- has its own direct test
    against REAL captured `gh pr list` output
    (``tests/fixtures/github/perf_kpi_pr_list_exchanges.json``), with no
    subprocess, no monkeypatch, and nothing simulated: this function never
    touches a process at all.
    """
    return stdout.strip() not in ("", "[]")


def _pr_already_open(repository: str, branch: str) -> bool:
    completed = subprocess.run(
        _pr_list_argv(repository, branch),
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
    return _parse_pr_list_result(completed.stdout)


def _create_pr_argv(repository: str, branch: str) -> list[str]:
    return [
        "gh",
        "pr",
        "create",
        "--repo",
        repository,
        "--base",
        "main",
        "--head",
        branch,
        "--title",
        LEDGER_PR_TITLE,
        "--body",
        "Standing docs PR for nightly perf KPI ledger appends. Never merges by itself.",
    ]


def _create_pr(repository: str, branch: str, *, cwd: Path) -> None:
    subprocess.run(_create_pr_argv(repository, branch), check=True, cwd=cwd)


def _update_ledger_pr_inner(repo_root: Path, ledger_path: Path, worktree_dir: Path) -> None:
    pr_already_open = _pr_already_open(REPOSITORY, LEDGER_PR_BRANCH)
    branch = LEDGER_PR_BRANCH
    subprocess.run(["git", "fetch", "origin", "main"], check=True, cwd=repo_root)
    # The branch is now ALWAYS fetched, and the lease taken from THAT same
    # fetched ref, whether or not a PR is currently open (claude-review, PR
    # #3827, round 6, P2): round 5 only fetched the branch when
    # pr_already_open, and derived the lease from a SEPARATE `git
    # ls-remote` run after that fetch. In the no-PR-open path, another host
    # could push a new entry -- or even open the PR itself -- between this
    # host's `gh pr list` check above and that ls-remote, and this host
    # would then take the other host's SHA as its own lease and force-push
    # over it. Fetching the branch unconditionally and reading the lease
    # via `git rev-parse` on the SAME fetched ref removes that window: the
    # branch fetch that determines `base_ref` IS the fetch the lease comes
    # from, not a second, later, independent round-trip to the remote.
    branch_fetch = subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if branch_fetch.returncode != 0 and "couldn't find remote ref" not in branch_fetch.stderr:
        raise RuntimeError(f"git fetch origin {branch} failed: {branch_fetch.stderr.strip()}")
    branch_exists_remotely = branch_fetch.returncode == 0
    if pr_already_open and not branch_exists_remotely:
        raise RuntimeError(
            f"gh pr list found an open PR for {branch}, but git fetch could not find that "
            "branch on origin -- inconsistent remote state, refusing to guess a base"
        )
    # Base on the branch whenever it EXISTS, not only when `gh pr list`
    # already sees a PR for it (claude-review, PR #3827, round 6, P2,
    # second half): the lease fix above only stops an UNNOTICED overwrite
    # -- it does nothing if the lease correctly reflects a branch that
    # moved, because a lease that matches reality lets the push through.
    # Keying base_ref on pr_already_open instead of branch_exists_remotely
    # meant a host that raced past `gh pr list` before another host both
    # pushed the branch AND opened its PR would still build fresh off
    # origin/main, take the other host's SHA as an accurate lease, and
    # force-push straight over that host's entry -- the lease "worked" and
    # the data loss happened anyway. Basing on the branch whenever it
    # exists means this run's commit is built ON TOP of whatever is
    # already there, so the push is a genuine fast-forward-shaped update
    # rather than a sibling history the lease would otherwise wave through.
    base_ref = f"origin/{branch}" if branch_exists_remotely else "origin/main"
    if branch_exists_remotely:
        lease_value = subprocess.run(
            ["git", "rev-parse", f"origin/{branch}"],
            check=True,
            capture_output=True,
            text=True,
            cwd=repo_root,
        ).stdout.strip()
    else:
        lease_value = ""
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
        # Reconcile a RETAINED branch with current main before anything is
        # appended (Codex, PR #3827, P2/BLOCKING): a branch can exist
        # remotely with no open PR because an earlier ledger PR already
        # merged (or was closed) without its head branch being deleted.
        # `base_ref` above still has to build from that branch, never from
        # origin/main, whenever the branch exists -- round 6's mid-race
        # fix depends on that to avoid force-pushing a sibling history
        # over a branch another host just pushed for real (see the
        # comment on `base_ref`). But a STALE retained branch's tree can
        # be arbitrarily far behind: real merge it forward onto
        # origin/main so the commit this run builds carries a tree that
        # matches current main everywhere except the ledger, instead of
        # reviving whatever main looked like when the branch was cut.
        # `-X ours` on conflicts because this branch, by construction,
        # never carries a change to any file this bot did not itself
        # commit -- docs/perf/kpi-ledger.json is the only file it ever
        # touches, and that file's real reconciliation is done explicitly
        # below via `_new_entries_since`, not left to git's text merge.
        # A no-op ("Already up to date") whenever the worktree was already
        # built from origin/main (branch_exists_remotely is False, or the
        # branch was already current).
        if branch_exists_remotely:
            merge = subprocess.run(
                ["git", "merge", "--no-edit", "-X", "ours", "origin/main"],
                cwd=worktree_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if merge.returncode != 0:
                raise RuntimeError(
                    f"git merge origin/main into {branch} failed: {merge.stderr.strip()}"
                )
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
        main_only_entries = _new_entries_since(branch_entries, main_entries)
        base_entries = branch_entries + main_only_entries
        new_entries = _new_entries_since(base_entries, local_entries)
        if not new_entries and not main_only_entries:
            return
        # Actually WRITE main_only_entries into dest (Codex, PR #3827,
        # P2/BLOCKING), not just fold them into the dedup comparison: the
        # union above only ever suppressed duplicate appends -- it never
        # copied main-only entries onto the branch's own published ledger,
        # so a retained stale branch's snapshot stayed missing everything
        # merged to main since it diverged. Appended first, in its own
        # commit, so the branch's history shows the reconciliation
        # separately from tonight's own new rows.
        if main_only_entries:
            append_entries(dest, main_only_entries, validate=False)
            subprocess.run(
                ["git", "add", "docs/perf/kpi-ledger.json"], check=True, cwd=worktree_dir
            )
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    f"perf(kpi): reconcile ledger with main ({len(main_only_entries)} entries)"
                    "\n\n-Codex",
                ],
                check=True,
                cwd=worktree_dir,
            )
        if new_entries:
            append_entries(dest, new_entries, validate=False)
            subprocess.run(
                ["git", "add", "docs/perf/kpi-ledger.json"], check=True, cwd=worktree_dir
            )
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
        _create_pr(REPOSITORY, branch, cwd=repo_root)


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
