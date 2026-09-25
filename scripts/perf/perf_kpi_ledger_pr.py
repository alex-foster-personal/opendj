"""Standing docs PR for nightly perf KPI ledger appends (issue #1506, PR #3827).

Publishes tonight's new ledger rows onto the standing `perf/kpi-nightly-ledger`
branch from a dedicated detached worktree (never REPO_ROOT's own checkout), with a
force-with-lease push and a merge onto main, then opens the PR if none is open.
REPO_ROOT-side state (restore, outbox, worktree cleanup) lives in
`perf_kpi_ledger_local`.

[if] two hosts publish the same night [then] both hosts' entries survive on the branch
and neither overwrites the other, [else stop].

-Claude
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.perf.kpi_ledger_append import append_entries, load_ledger
from scripts.perf.perf_kpi_config import LEDGER_PR_BRANCH, LEDGER_PR_TITLE
from scripts.perf.perf_kpi_ledger_local import (
    _archive_unpublished_ledger,
    _clear_outbox,
    _load_outbox_entries,
    _new_entries_since,
    _remove_worktree_if_present,
    _restore_tracked_ledger,
)

REPOSITORY = "maintainer/music-dj-tools"


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
    *,
    pre_run_content: str | None,
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

    ``pre_run_content`` has no default either, for the same reason (Sol, PR
    #3827, P1/BLOCKING, review 4107678114): it is REPO_ROOT's own tracked
    ledger content from immediately before `run_nightly` ran, which only the
    caller (`cmd_nightly`) can capture at the right moment -- by the time
    this function is entered, `run_nightly` has already appended tonight's
    rows directly into the file, so reading it here would just capture that
    already-mutated state, not the caller's true prior one.

    Restores REPO_ROOT's own tracked ledger to exactly ``pre_run_content``
    afterward once tonight's rows are durably published or archived (Codex /
    Sol, PR #3827, P1/BLOCKING): see ``_restore_tracked_ledger``. If the
    archive itself fails, the tracked file is deliberately left as it is.

    Publishes only entries genuinely new since
    ``pre_run_content``, folded together with anything an earlier run
    failed to publish, and archives that same set to an outbox on failure
    (Sol, PR #3827, P1/BLOCKING x2): see ``_update_ledger_pr_inner``.
    """
    _update_ledger_pr_inner(repo_root, ledger_path, worktree_dir, pre_run_content=pre_run_content)


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

    Parses and validates the JSON contract rather than treating any
    non-empty stdout as "a PR exists" (Sol, PR #3827, P1/BLOCKING, review
    5321908943, "Validate the gh PR-list response before deciding a PR
    exists"): malformed JSON or a drifted shape such as ``null`` or ``{}``
    used to read as an open PR, so a remotely existing branch was updated
    while `_create_pr` was silently skipped. Only a list of objects, each
    with an integer ``number``, is an answer; everything else raises.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"gh pr list returned non-JSON output: {stdout!r}") from exc
    if not isinstance(payload, list):
        raise TypeError(f"gh pr list returned {type(payload).__name__}, not a list: {stdout!r}")
    for item in payload:
        number = item.get("number") if isinstance(item, dict) else None
        if not isinstance(number, int) or isinstance(number, bool):
            raise TypeError(f"gh pr list returned an item without an integer number: {item!r}")
    return bool(payload)


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


def _update_ledger_pr_inner(
    repo_root: Path, ledger_path: Path, worktree_dir: Path, *, pre_run_content: str | None
) -> None:
    """Compute this run's true publish candidates and archive them on failure.

    Computed FIRST, before anything below that can fail (a network fetch, a
    worktree operation, the push itself), so a failure at any point still
    has a captured candidate set to archive -- the candidates are read from
    REPO_ROOT's own `ledger_path` directly, independent of the worktree
    machinery `_update_ledger_pr_publish` sets up afterward.

    Publishes only entries genuinely new since ``pre_run_content`` (Sol, PR
    #3827, P1/BLOCKING, "Publish only entries created by the nightly run"):
    the tracked ledger at call time can hold more than tonight's own rows --
    any entry an operator or another process already had sitting there,
    uncommitted, before this job even started. `_restore_tracked_ledger`
    preserves that pre-existing edit exactly as it was; it must never also
    be folded into what gets pushed to the standing branch.

    Folds in whatever an earlier run's own entries never made it onto the
    branch (Sol, PR #3827, P1/BLOCKING, "Retain unpublished entries until
    they are successfully published"): see `_load_outbox_entries` /
    `_archive_unpublished_ledger`. A second consecutive failure re-archives
    the FULL still-pending set, so nothing from the first failure is ever
    silently dropped by the second.
    """
    # Snapshot of the file exactly as this run left it; the restore only
    # replaces a file that still matches it (review comment 4108252023).
    post_run_content = ledger_path.read_text(encoding="utf-8")
    local_entries = load_ledger(ledger_path)["entries"]
    pre_run_entries = json.loads(pre_run_content)["entries"] if pre_run_content else []
    tonight_entries = _new_entries_since(pre_run_entries, local_entries)
    outbox_dir = worktree_dir.parent / "unpublished-ledger-outbox"
    outbox_entries = _load_outbox_entries(outbox_dir)
    candidate_entries = tonight_entries + _new_entries_since(tonight_entries, outbox_entries)
    # REPO_ROOT's tracked ledger is restored ONLY once tonight's rows are
    # durably somewhere else -- on the branch, or in a completely written
    # outbox (Sol, PR #3827, P1/BLOCKING, review 5321908943, "Preserve
    # nightly rows when outbox persistence fails"). The old unconditional
    # `finally` restore also ran when candidate preparation or the archive
    # write itself raised (a corrupt outbox, a permission error, a full
    # disk), wiping tonight's rows from the only place they were left. Any
    # such failure now propagates with the tracked file left as it is.
    try:
        _update_ledger_pr_publish(repo_root, worktree_dir, candidate_entries)
    except Exception as publish_error:
        _archive_unpublished_ledger(candidate_entries, outbox_dir=outbox_dir)
        try:
            _restore_tracked_ledger(
                repo_root,
                ledger_path,
                pre_run_content=pre_run_content,
                post_run_content=post_run_content,
            )
        except RuntimeError as restore_refusal:
            # Keep the publish failure as the primary error; the refusal rides along.
            publish_error.add_note(str(restore_refusal))
        raise
    else:
        _clear_outbox(outbox_dir)
        _restore_tracked_ledger(
            repo_root,
            ledger_path,
            pre_run_content=pre_run_content,
            post_run_content=post_run_content,
        )


def _update_ledger_pr_publish(repo_root: Path, worktree_dir: Path, candidate_entries: list) -> None:
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
        new_entries = _new_entries_since(base_entries, candidate_entries)
        if not new_entries and not main_only_entries:
            return
        # append_entries validates by default now (Sol, PR #3827, P1/BLOCKING,
        # review 4107678137, "Validate ledger entries before committing
        # them"): both calls below used to pass validate=False, so a
        # malformed entry already sitting on main or on the local ledger
        # would get committed and pushed onto the standing branch as-is,
        # deferring the failure to whatever later consumer tried to read it.
        # Every entry reconciled here already came from a REAL ledger
        # (main's or local's), which only ever gets new rows through this
        # same validated append path, so re-validating on the way onto the
        # branch is a cheap, always-safe sanity check, not new strictness.
        #
        # Actually WRITE main_only_entries into dest (Codex, PR #3827,
        # P2/BLOCKING), not just fold them into the dedup comparison: the
        # union above only ever suppressed duplicate appends -- it never
        # copied main-only entries onto the branch's own published ledger,
        # so a retained stale branch's snapshot stayed missing everything
        # merged to main since it diverged. Appended first, in its own
        # commit, so the branch's history shows the reconciliation
        # separately from tonight's own new rows.
        if main_only_entries:
            append_entries(dest, main_only_entries)
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
            append_entries(dest, new_entries)
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
