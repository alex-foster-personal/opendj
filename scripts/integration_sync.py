#!/usr/bin/env python3
"""Keep the version trunks (main-v2, main-v3, ...) merged forward from their upstream.

ADR-NEW-post-v1-branch-strategy. Every long-lived integration branch this repo has
had (codex--v2-integration, codex--first-tester--integration, af--windows-integration,
main-electron-rust) went thousands of commits behind main within days and stopped
being mergeable. This job is the forward-sync that keeps a version trunk close to
its upstream, so the eventual promotion PR stays small.

How one trunk is synced, all through the REST API (no push from the runner):

1. Create `sync--<trunk>--<upstream-sha12>` at the trunk tip.
2. Ask GitHub to merge the upstream SHA into that branch (POST /merges). GitHub
   writes the merge commit, or answers 409 on a conflict.
3. Dispatch ci.yml (tier full) on the sync branch. A dispatch checks out the
   branch head as is, and that head IS the merge commit, so the run tests the
   exact commit the trunk would advance to.
4. On a later pass, when every required check on that commit concluded success
   and the commit still contains the trunk tip, fast-forward the trunk to it
   (PATCH refs, force=false). Only when INTEGRATION_SYNC_AUTO_FF is true. That
   later pass is usually the next hourly advance-only run, so a green sync lands
   within the hour, not a day later.
5. If the trunk moved while the sync was pending, merge the new trunk tip into
   the sync branch and dispatch CI again, rather than dropping it: a busy trunk
   would otherwise never hold still long enough for a sync to land.

Nothing is written unless INTEGRATION_SYNC_WRITES is true; until then every pass
is a dry run that only reports.

MINI-PRD
    R1 Up to date ..................................................... done + regression
       [if] the trunk already contains the upstream tip [then] do nothing, report
            up-to-date [else stop]
    R2 Start a sync ................................................... done + regression
       [if] the trunk is behind and no sync branch is pending for it
            [then] create one, merge the upstream tip into it, dispatch CI [else stop]
       [if] the merge conflicts [then] delete the sync branch, create
            `sync--<trunk>--conflict--<sha12>` at the upstream tip, open a PR from it into
            the trunk naming the conflicted paths, exit 1 [else stop]
       [if] a conflict PR into the trunk is open [then] skip the trunk until it
            merges [else stop]
       [if] anything would need a force-push [then] it is never done: every ref
            write is a create, a delete of a bot branch, or force=false [else stop]
    R3 Advance ........................................................ done + regression
       [if] auto fast-forward is on, a sync commit contains the trunk tip
            and every required check is PRESENT with conclusion success
            [then] fast-forward the trunk to it [else stop]
       [if] a required check is missing or still running [then] wait, never advance
            [else stop]
    R4 Stale, stuck and red ........................................... done + regression
       [if] the trunk moved past the current sync branch [then] merge the new trunk tip
            into it and dispatch CI again; on a conflict delete it [else stop]
       [if] the trunk moved past a sync branch for an older upstream tip [then] delete
            it [else stop]
       [if] no required check has any run STUCK_HOURS after the sync commit [then]
            delete the branch and report red, exit 1, so the next pass starts over
            [else stop]
       [if] a required check concluded anything but success [then] report red,
            exit 1, keep the branch for diagnosis until a newer upstream tip replaces
            it [else stop]
    R5 Preconditions .................................................. done + regression
       [if] a configured trunk or upstream does not exist, or the API answers
            something unexpected [then] exit 10 with UNKNOWN, never a verdict, and
            still sync every other trunk [else stop]
    R6 Squashed conflict PR ........................................... done + regression
       [if] any merged conflict PR's head is not contained in the trunk
            (merged by squash or rebase) [then] report red, exit 1, and skip the
            trunk: every later merge would re-conflict on the same paths [else stop]
    R7 Writes switch .................................................. done + regression
       [if] INTEGRATION_SYNC_WRITES is not true [then] every pass is a dry run [else stop]

USAGE
    uv run --no-sync python -m scripts.integration_sync --dry-run
    INTEGRATION_SYNC_WRITES=true uv run --no-sync python -m scripts.integration_sync
    INTEGRATION_SYNC_WRITES=true uv run --no-sync python -m scripts.integration_sync --advance-only

-Claude
"""

from __future__ import annotations

import argparse
import json
import os
import re  # noqa: F401  # re-exported: kept importable from before the split
import shutil  # noqa: F401  # re-exported: tests patch sync.shutil.which
import subprocess
import sys
from collections.abc import Callable  # noqa: F401  # re-exported
from dataclasses import dataclass, field  # noqa: F401  # re-exported
from datetime import UTC, datetime  # noqa: F401  # re-exported
from pathlib import Path
from typing import Any  # noqa: F401  # re-exported

# Split under #10976: the pure plan and the GitHub boundary live in sibling modules.
# Everything stays importable from here. conflicted_paths and PATH_LIST_LIMIT are looked
# up in this module's globals by _apply_create, so a monkeypatch on this module is seen.
from scripts.integration_sync_github import (  # noqa: F401  # re-exported: public surface
    CHECK_RUN_PAGE_CAP,
    GhCall,
    Repo,
    _is_conflict_pr,
    _resolve,
    conflicted_paths,
    gh_call,
)
from scripts.integration_sync_plan import (  # noqa: F401  # re-exported: public surface
    STUCK_HOURS,
    SYNC_PREFIX,
    Action,
    PreconditionError,
    SyncRef,
    _is_sync_ref,
    _plan_live,
    _plan_moved_trunk,
    conflict_ref_name,
    is_green,
    is_red,
    is_stuck,
    plan_trunk,
    red_note,
    sync_ref_name,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "ci" / "integration-trunks.json"
EXIT_OK = 0
EXIT_NEEDS_HUMAN = 1
EXIT_PRECONDITION = 10
# A PR body stays well under GitHub's 65536-character limit with this much of the conflicted-path list.
PATH_LIST_LIMIT = 30_000


def run_trunk(
    repo: Repo,
    trunk: str,
    upstream: str,
    required: list[str],
    auto_ff: bool,
    dry_run: bool,
    *,
    advance_only: bool = False,
    advanced: set[str] | None = None,
) -> tuple[int, list[str]]:
    """Sync one trunk. `advanced` collects every trunk this pass advances, recorded at the moment
    each fast-forward lands, so a later failure in the pass cannot hide it from downstream trunks."""
    trunk_sha = repo.ref_sha(trunk)
    upstream_sha = repo.ref_sha(upstream)
    waiting = repo.open_conflict_prs(trunk)
    if waiting:
        # One conflict at a time: a newer sync would conflict on the same paths.
        shown = ", ".join(f"#{n}" for n in waiting)
        return (
            EXIT_OK,
            [f"| {trunk} | waiting on conflict PR {shown} | resolve and merge it; sync resumes after |"],
        )
    for resolved in repo.merged_conflict_prs(trunk):
        head = resolved["head"]["sha"]
        if not repo.contains(trunk_sha, head):
            return (
                EXIT_NEEDS_HUMAN,
                [
                    f"| {trunk} | RED conflict PR #{resolved['number']} landed without its history | "
                    f"squash or rebase merge kept the content but not `{head[:12]}`, so every sync re-conflicts; "
                    f"merge `{head[:12]}` into {trunk} with a merge commit |"
                ],
            )
    behind = repo.behind_by(trunk_sha, upstream_sha)
    refs = repo.sync_refs(trunk, trunk_sha, required)
    actions = plan_trunk(
        trunk=trunk,
        trunk_sha=trunk_sha,
        upstream_sha=upstream_sha,
        behind_by=behind,
        sync_refs=refs,
        required=required,
        auto_ff=auto_ff,
        advance_only=advance_only,
    )
    code, lines, landed = _apply_actions(repo, actions, trunk, upstream, trunk_sha, upstream_sha, dry_run, advanced)
    if landed and not dry_run:
        # The advance moved the trunk to an older upstream's green sync, so the plan made for the
        # old tip is stale. Re-plan once on the new tip, in every mode: that starts the latest
        # upstream tip's sync, or rebuilds a live one whose first parent is now the old trunk, so the
        # trunk does not wait for the next daily pass. One round only; the next pass continues.
        fresh_trunk = landed
        behind = repo.behind_by(fresh_trunk, upstream_sha)
        refs = repo.sync_refs(trunk, fresh_trunk, required)
        again = plan_trunk(
            trunk=trunk,
            trunk_sha=fresh_trunk,
            upstream_sha=upstream_sha,
            behind_by=behind,
            sync_refs=refs,
            required=required,
            auto_ff=auto_ff,
            advance_only=False,
        )
        more_code, more_lines, _ = _apply_actions(
            repo, again, trunk, upstream, fresh_trunk, upstream_sha, dry_run, advanced
        )
        code = max(code, more_code)
        lines += more_lines
    return code, lines


def _apply_actions(
    repo: Repo,
    actions: list[Action],
    trunk: str,
    upstream: str,
    trunk_sha: str,
    upstream_sha: str,
    dry_run: bool,
    advanced: set[str] | None = None,
) -> tuple[int, list[str], str | None]:
    """Run the trunk's planned actions in order. Returns the code, the rows, and the sha the trunk
    landed on, or None when no advance landed. A refused advance stops the trunk here: the sync
    branch it would have moved from stays put, and nothing downstream is started from it."""
    code = EXIT_OK
    lines = []
    landed = None
    for act in actions:
        if act.kind == "create":
            act_code, line = _apply_create(repo, act, trunk, upstream, trunk_sha, upstream_sha, dry_run)
        elif act.kind == "refresh":
            act_code, line = _apply_refresh(repo, act, trunk, upstream, trunk_sha, upstream_sha, dry_run)
        else:
            act_code, line = _apply_simple(repo, act, trunk, upstream, trunk_sha, dry_run)
        if act.kind == "advance" and not dry_run:
            if act_code != EXIT_OK:
                code = max(code, act_code)
                lines.append(line)
                break
            # Recorded here, once the fast-forward has landed: a later step may still raise.
            landed = act.sha
            if advanced is not None:
                advanced.add(trunk)
        code = max(code, act_code)
        lines.append(line)
    return code, lines, landed


def _refused_delete(repo: Repo, act: Action, trunk: str, upstream: str) -> str | None:
    """Delete a planned sync branch. A refused delete (the branch moved, or is not a sync merge)
    comes back as its UNKNOWN row, so the rest of the trunk's pass still runs."""
    try:
        repo.delete_ref_at(act.ref, act.sha, trunk, upstream)
    except PreconditionError as err:
        return f"| {trunk} | UNKNOWN, delete refused: {err} | {act.note} |"
    return None


def _apply_delete(repo: Repo, act: Action, trunk: str, upstream: str, dry_run: bool) -> tuple[int, str]:
    """Delete a planned sync branch: superseded, stuck, or about to be rebuilt. A refused delete is UNKNOWN."""
    verb = "would " if dry_run else ""
    if not dry_run and (refused := _refused_delete(repo, act, trunk, upstream)):
        return EXIT_PRECONDITION, refused
    if act.kind == "stuck":
        return EXIT_NEEDS_HUMAN, f"| {trunk} | RED {verb}delete `{act.ref}`, its CI never reported | {act.note} |"
    return EXIT_OK, f"| {trunk} | {verb}delete `{act.ref}` | {act.note} |"


def _apply_simple(repo: Repo, act: Action, trunk: str, upstream: str, trunk_sha: str, dry_run: bool) -> tuple[int, str]:
    verb = "would " if dry_run else ""
    if act.kind in ("delete", "stuck"):
        return _apply_delete(repo, act, trunk, upstream, dry_run)
    if act.kind == "advance":
        if not dry_run:
            try:
                repo.fast_forward_from(trunk, act.sha, trunk_sha)
            except PreconditionError as err:
                return EXIT_PRECONDITION, f"| {trunk} | UNKNOWN, advance refused: {err} | {act.note} |"
        return EXIT_OK, f"| {trunk} | {verb}fast-forward to `{act.sha[:12]}` | {act.note} |"
    if act.kind == "wait":
        return EXIT_OK, f"| {trunk} | waiting on `{act.ref}` | {act.note} |"
    if act.kind == "red":
        return EXIT_NEEDS_HUMAN, f"| {trunk} | RED `{act.ref}` | {act.note}; fix on a branch into {trunk} |"
    if act.kind == "up-to-date":
        return EXIT_OK, f"| {trunk} | up to date | {act.note} |"
    if act.kind == "idle":
        return EXIT_OK, f"| {trunk} | nothing pending | {act.note} |"
    raise PreconditionError(f"unknown action {act.kind}")


def _apply_create(
    repo: Repo, act: Action, trunk: str, upstream: str, trunk_sha: str, upstream_sha: str, dry_run: bool
) -> tuple[int, str]:
    tip = f"{upstream}@`{upstream_sha[:12]}`"
    if dry_run:
        return EXIT_OK, f"| {trunk} | would merge {tip} | {act.note} |"
    repo.create_ref(act.ref, trunk_sha)
    status, merge_sha = repo.merge(
        act.ref, upstream_sha, f"chore(sync): merge {upstream}@{upstream_sha[:12]} into {trunk}"
    )
    if status == 201:
        message = f"chore(sync): merge {upstream}@{upstream_sha[:12]} into {trunk}"
        merge_sha = repo.with_trunk_first_parent(act.ref, merge_sha, trunk_sha, upstream_sha, message)
        _dispatch_or_drop(repo, act.ref, merge_sha, trunk, upstream)
        return EXIT_OK, f"| {trunk} | merged {tip} into `{act.ref}` as `{merge_sha[:12]}`, CI dispatched | {act.note} |"
    # Created a moment ago at the trunk tip, so it holds no sync merge yet to check for.
    repo.delete_ref_at(act.ref, trunk_sha, trunk, upstream, sync_merge=False)
    if status == 204:
        # The compare said the trunk is behind, so GitHub's "nothing to merge" contradicts it. Trust
        # only a trunk that already contains the upstream tip; anything else is UNKNOWN, not green.
        if repo.contains(trunk_sha, upstream_sha):
            return EXIT_OK, f"| {trunk} | nothing to merge | {trunk} already contains {upstream_sha[:12]} |"
        raise PreconditionError(
            f"merge answered 204 but {trunk} does not contain {upstream_sha[:12]}; the trunk moved or GitHub disagrees with the compare"
        )
    paths = conflicted_paths(trunk_sha, upstream_sha)
    if not paths:
        # A conflict PR must name the paths it conflicts on. Unmeasured, or measured as none, the
        # sync is UNKNOWN and the next pass retries it, rather than a PR that names no paths.
        raise PreconditionError(
            f"merge of {upstream_sha[:12]} into {trunk} answered 409 but its conflicted paths could not be measured; "
            "no conflict PR opened, the next pass retries"
        )
    shown = ", ".join(paths)
    if len(shown) > PATH_LIST_LIMIT:
        # The PR body must name every conflicted path or none is named as a fact: refuse an oversized list
        # rather than cut it, so the trunk stays UNKNOWN and a person lists the paths by hand.
        raise PreconditionError(
            f"merge of {upstream_sha[:12]} into {trunk} conflicts on {len(paths)} paths, too many to list in a PR body; "
            "no conflict PR opened"
        )
    return (
        EXIT_NEEDS_HUMAN,
        f"| {trunk} | CONFLICT merging {tip}; {_open_conflict_pr(repo, trunk, upstream, upstream_sha, shown)} | {shown} |",
    )


def _dispatch_or_drop(repo: Repo, ref: str, sha: str, trunk: str, upstream: str) -> None:
    """Dispatch CI on a sync branch; if that fails, delete the branch so it cannot
    sit untested and block the trunk, and re-raise as UNKNOWN."""
    try:
        repo.dispatch_ci(ref)
    except PreconditionError:
        repo.delete_ref_at(ref, sha, trunk, upstream)
        raise


def _apply_refresh(
    repo: Repo, act: Action, trunk: str, upstream: str, trunk_sha: str, upstream_sha: str, dry_run: bool
) -> tuple[int, str]:
    """Delete the stale sync branch and build it again from the new trunk tip, so the
    trunk tip stays the first parent. A conflict now opens a conflict PR at once."""
    if dry_run:
        return EXIT_OK, f"| {trunk} | would rebuild `{act.ref}` on {trunk}@`{trunk_sha[:12]}` | {act.note} |"
    if refused := _refused_delete(repo, act, trunk, upstream):
        return EXIT_PRECONDITION, refused
    code, line = _apply_create(repo, act, trunk, upstream, trunk_sha, upstream_sha, dry_run)
    return code, line.replace(f"| {trunk} | ", f"| {trunk} | rebuilt on {trunk}@`{trunk_sha[:12]}`: ", 1)


def _conflict_branch(repo: Repo, trunk: str, upstream_sha: str) -> str:
    """The first conflict branch name for this upstream tip that is either free (created
    now at the upstream tip) or already at it. A branch left behind by a closed, unmerged
    conflict PR carries that resolver's commits, so it is never reused: the next name is tried."""
    base = conflict_ref_name(trunk, upstream_sha)
    for n in range(1, 21):
        candidate = base if n == 1 else f"{base}-{n}"
        existing = repo.ref_sha_or_none(candidate)
        if existing is None:
            repo.create_ref(candidate, upstream_sha)
            return candidate
        if existing == upstream_sha:
            return candidate
    raise PreconditionError(f"no free conflict branch name for {base}")


def _open_conflict_pr(repo: Repo, trunk: str, upstream: str, upstream_sha: str, shown: str) -> str:
    """Open a PR from a branch at the upstream tip into the trunk. Never a force-push:
    the branch is new, and whoever resolves it merges the trunk into it and pushes."""
    head = _conflict_branch(repo, trunk, upstream_sha)
    body = "\n".join(
        [
            f"The daily integration sync could not merge `{upstream}` @ `{upstream_sha[:12]}` into `{trunk}`.",
            "",
            f"Conflicted paths: {shown}",
            "",
            "To resolve, on this branch:",
            "",
            "```",
            # Explicit refspecs: a single-branch clone fetches other branch names into FETCH_HEAD
            # only, so without these the next checkout finds no branch to check out.
            f"git fetch origin refs/heads/{trunk}:refs/remotes/origin/{trunk} "
            f"refs/heads/{head}:refs/remotes/origin/{head}",
            # -b fails when the local branch exists, so unpushed commits are never reset.
            # To resume an existing local branch instead: git switch {head}
            f"git checkout -b {head} origin/{head}",
            f"git merge origin/{trunk}",
            "# fix the conflicts, commit, then a normal push (never --force)",
            f"git push origin {head}",
            "```",
            "",
            f"CI runs on that push. Merge this PR into `{trunk}` once `ci gate` is green, with a",
            "**merge commit, never squash or rebase**: the sync needs this branch's history in the",
            "trunk, or every later sync re-conflicts on the same paths (the job reports that as red).",
            f"The sync job skips `{trunk}` while this PR is open and resumes on its next pass after it merges.",
            "",
            "ADR-NEW-post-v1-branch-strategy, `scripts/integration_sync.py`.",
        ]
    )
    status, data = repo.open_pr(head, trunk, f"chore(sync): resolve {upstream} into {trunk} conflict", body)
    if status == 201 and isinstance(data, dict):
        return f"opened PR #{data['number']}"
    return f"could NOT open a PR ({status}: {data}); branch `{head}` is ready for a manual PR"


def load_config(path: Path) -> tuple[list[str], list[tuple[str, str]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = list(data.get("required_checks") or [])
    trunks = [(t["branch"], t["upstream"]) for t in data.get("trunks") or []]
    if not required or not trunks:
        raise PreconditionError(f"{path} names no required checks or no trunks")
    return required, trunks


def _current_repo() -> str:
    """The repository the local gh login resolves for this checkout. Outside Actions,
    GITHUB_REPOSITORY is normally unset, so a local dry run asks gh instead."""
    gh = _resolve("gh")
    if gh is None:
        raise PreconditionError("no --repo, no GITHUB_REPOSITORY, and no gh on PATH")
    proc = subprocess.run(
        [gh, "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
        check=False,
    )
    slug = proc.stdout.strip()
    if proc.returncode != 0 or not slug:
        raise PreconditionError(f"gh repo view failed ({proc.returncode}): {proc.stderr.strip()[:200]}")
    return slug


def _repo_from(slug: str) -> Repo:
    return Repo(slug or _current_repo())


def _require_known_only(only: str, trunks: list[tuple[str, str]]) -> None:
    """A mistyped --only would skip every trunk and still report green, so it is an error."""
    if only and only not in {trunk for trunk, _ in trunks}:
        raise PreconditionError(f"--only {only!r} matches no trunk in the config")


def main(argv: list[str] | None = None, repo: Repo | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", help="sync one trunk by branch name")
    parser.add_argument(
        "--advance-only",
        action="store_true",
        help="advance, refresh and clean up pending syncs; start a sync only when this run advances one",
    )
    args = parser.parse_args(argv)
    auto_ff = os.environ.get("INTEGRATION_SYNC_AUTO_FF", "") == "true"
    writes = os.environ.get("INTEGRATION_SYNC_WRITES", "") == "true"
    dry_run = args.dry_run or not writes

    rows = ["| trunk | action | detail |", "| --- | --- | --- |"]
    code = EXIT_OK
    advanced_upstreams: set[str] = set()
    try:
        required, trunks = load_config(args.config)
        _require_known_only(args.only, trunks)
        repo = repo or _repo_from(args.repo)
    except PreconditionError as exc:
        rows.append(f"| - | UNKNOWN | {exc} |")
        trunks, code = [], EXIT_PRECONDITION
    for trunk, upstream in trunks:
        if args.only and trunk != args.only:
            continue
        # An upstream that advanced earlier in this pass has new commits for this trunk now, so
        # its sync starts here rather than waiting for the daily pass.
        upstream_moved = upstream in advanced_upstreams
        try:
            assert repo is not None
            trunk_code, lines = run_trunk(
                repo,
                trunk,
                upstream,
                required,
                auto_ff,
                dry_run,
                advance_only=args.advance_only and not upstream_moved,
                advanced=advanced_upstreams,
            )
        except PreconditionError as exc:
            # One trunk that cannot be measured must not stop the others.
            trunk_code, lines = EXIT_PRECONDITION, [f"| {trunk} | UNKNOWN | {exc} |"]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            # A GitHub answer whose shape the reads did not expect. Every read validates what it
            # can; this catches the rest of the boundary, so the trunk reports UNKNOWN, not a crash.
            trunk_code = EXIT_PRECONDITION
            lines = [f"| {trunk} | UNKNOWN | unexpected GitHub payload: {type(exc).__name__}: {exc} |"]
        rows += lines
        code = max(code, trunk_code)
    modes = [f"auto fast-forward {'on' if auto_ff else 'off'}"]
    if not writes:
        modes.append("writes off (INTEGRATION_SYNC_WRITES), dry run")
    elif args.dry_run:
        modes.append("dry run")
    if args.advance_only:
        modes.append("advance only")
    header = f"### Integration sync ({', '.join(modes)})"
    report = "\n".join([header, "", *rows, ""])
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
