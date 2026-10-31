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
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "ci" / "integration-trunks.json"
SYNC_PREFIX = "sync"
EXIT_OK = 0
EXIT_NEEDS_HUMAN = 1
EXIT_PRECONDITION = 10
# A PR body stays well under GitHub's 65536-character limit with this much of the conflicted-path list.
PATH_LIST_LIMIT = 30_000
# Pages of 100 check runs read per commit before giving up as UNKNOWN.
CHECK_RUN_PAGE_CAP = 20
# Hours a sync commit may sit with no run of any required check before the job
# gives up on its CI (dispatch lost, run cancelled before the gate, no runner).
STUCK_HOURS = 6


class PreconditionError(RuntimeError):
    """The job could not measure; it must report UNKNOWN, never a verdict."""


@dataclass(frozen=True)
class SyncRef:
    """One bot-owned `sync--<trunk>--<sha12>` branch and what is known about its head."""

    name: str
    sha: str
    # The head is a sync merge of the trunk tip (FIRST parent) and the upstream tip its name
    # carries (SECOND parent), so a fast-forward to it keeps the trunk's first-parent history
    # and lands only the upstream this branch was made for. False once the trunk moved after
    # the merge, or when the head is not that merge (a stale, corrupted or hand-made branch).
    on_trunk_tip: bool
    # Required check name -> conclusion ("success", "failure", ...) or None while
    # the run is queued or in progress. A required check with NO run is absent
    # from the mapping, which reads as pending, never as passed.
    checks: dict[str, str | None] = field(default_factory=dict)
    # Committer date of the head commit, for the stuck-CI cap.
    committed_at: datetime | None = None


@dataclass(frozen=True)
class Action:
    kind: str  # up-to-date | create | advance | delete | refresh | wait | stuck | red | idle
    ref: str = ""
    sha: str = ""
    note: str = ""


def sync_ref_name(trunk: str, upstream_sha: str) -> str:
    # `--` separators, not `/`: the repo is migrating its branch names off slashes.
    return f"{SYNC_PREFIX}--{trunk}--{upstream_sha[:12]}"


def conflict_ref_name(trunk: str, upstream_sha: str) -> str:
    return f"{SYNC_PREFIX}--{trunk}--conflict--{upstream_sha[:12]}"


def _is_sync_ref(name: str, trunk: str) -> bool:
    """`sync--<trunk>--<sha12>` exactly; a conflict branch or a longer trunk name is not ours."""
    prefix = f"{SYNC_PREFIX}--{trunk}--"
    rest = name.removeprefix(prefix)
    return name.startswith(prefix) and len(rest) == 12 and all(c in "0123456789abcdef" for c in rest)


def is_green(ref: SyncRef, required: list[str]) -> bool:
    """Presence of success for every required check, not absence of failure."""
    if not required:
        raise PreconditionError("no required checks configured; a sync would advance untested")
    return all(ref.checks.get(name) == "success" for name in required)


def is_red(ref: SyncRef, required: list[str]) -> bool:
    return any(
        name in ref.checks and ref.checks[name] is not None and ref.checks[name] != "success" for name in required
    )


def red_note(ref: SyncRef, required: list[str]) -> str:
    """Names each required check that concluded non-success, so an operator can see which gate to repair."""
    failed = [f"{name} {ref.checks[name]}" for name in required if is_red(ref, [name])]
    return "required check failed at this upstream tip: " + ", ".join(failed)


def is_stuck(ref: SyncRef, required: list[str], now: datetime) -> bool:
    """No required check has any run, long after the sync commit was made."""
    if any(name in ref.checks for name in required) or ref.committed_at is None:
        return False
    return (now - ref.committed_at).total_seconds() > STUCK_HOURS * 3600


def plan_trunk(
    *,
    trunk: str,
    trunk_sha: str,
    upstream_sha: str,
    behind_by: int,
    sync_refs: list[SyncRef],
    required: list[str],
    auto_ff: bool,
    advance_only: bool = False,
) -> list[Action]:
    """Pure decision for one trunk. IO happens in run_trunk."""
    current = sync_ref_name(trunk, upstream_sha)
    actions, live = _plan_moved_trunk(sync_refs, current, trunk_sha)
    if actions and actions[-1].kind == "refresh":
        if behind_by > 0:
            return actions
        # The trunk already holds this upstream tip (R1: a no-op), so the pending sync is not needed.
        # Drop it rather than rebuild it and dispatch a redundant CI run.
        stale = actions.pop()
        actions.append(Action("delete", stale.ref, stale.sha, "trunk already contains the upstream tip"))
        actions.append(Action("up-to-date", note=f"{trunk} contains {upstream_sha[:12]}"))
        return actions
    done = _plan_live(live, required, auto_ff, current, actions)
    if done:
        return actions
    if behind_by == 0:
        actions.append(Action("up-to-date", note=f"{trunk} contains {upstream_sha[:12]}"))
    elif advance_only:
        actions.append(Action("idle", note=f"{behind_by} upstream commits wait for the daily pass"))
    else:
        actions.append(Action("create", current, upstream_sha, f"{behind_by} upstream commits to merge"))
    return actions


def _plan_moved_trunk(sync_refs: list[SyncRef], current: str, trunk_sha: str) -> tuple[list[Action], list[SyncRef]]:
    """Split sync branches into live ones and the ones the trunk moved past."""
    actions: list[Action] = []
    live: list[SyncRef] = []
    for ref in sync_refs:
        if ref.on_trunk_tip:
            live.append(ref)
        elif ref.name == current:
            # Still the sync this trunk needs: rebuild it on the new trunk tip in this
            # same pass, so a busy trunk cannot starve it. Rebuilt, not merged into:
            # merging the trunk into the sync branch would make the OLD sync head the
            # first parent and bend the trunk's first-parent history on advance.
            actions.append(Action("refresh", ref.name, ref.sha, f"trunk moved to {trunk_sha[:12]}; rebuilding on it"))
            return actions, []
        else:
            actions.append(Action("delete", ref.name, ref.sha, "trunk moved past it, and upstream has moved on"))
    return actions, live


def _plan_live(live: list[SyncRef], required: list[str], auto_ff: bool, current: str, actions: list[Action]) -> bool:
    """Append what to do with the live sync branches; True when nothing new may start."""
    for ref in live:
        if is_green(ref, required):
            if auto_ff:
                actions.append(Action("advance", ref.name, ref.sha, "required checks green"))
                # The trunk moves this pass; the next pass plans from its new tip.
                actions.append(Action("delete", ref.name, ref.sha, "advanced"))
            else:
                actions.append(Action("wait", ref.name, ref.sha, "green; auto fast-forward is off"))
            return True

    now = datetime.now(UTC)
    pending = [r for r in live if not is_red(r, required)]
    stuck = [r for r in pending if is_stuck(r, required, now)]
    note = f"no {' / '.join(required)} run {STUCK_HOURS}h after the sync commit; deleted so the next pass retries"
    actions.extend(Action("stuck", r.name, r.sha, note) for r in stuck)
    pending = [r for r in pending if r not in stuck]
    if pending:
        actions.extend(Action("wait", r.name, r.sha, "required checks not finished") for r in pending)
        return True
    # A stuck branch is deleted above and not rebuilt in this pass's plan, so a fresh sync may start in the same
    # pass rather than waiting for the next daily run.
    for ref in (r for r in live if is_red(r, required)):
        if ref.name == current:
            actions.append(Action("red", ref.name, ref.sha, red_note(ref, required)))
            return True
        actions.append(Action("delete", ref.name, ref.sha, "red, superseded by a newer upstream tip"))
    return False


# ---------------------------------------------------------------------------
# GitHub boundary


GhCall = Callable[[str, str, dict | None], tuple[int, object]]


def _resolve(name: str) -> str | None:
    """The absolute path of an executable, looked up once; the call then runs that path,
    so a PATH change between steps cannot swap in a different binary."""
    return shutil.which(name)


def gh_call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    """`gh api` with the HTTP status kept, so 409 and 204 are answers, not crashes."""
    gh = _resolve("gh")
    if gh is None:
        raise PreconditionError("gh is not on PATH")
    cmd = [gh, "api", "-i", "-X", method, path]
    if body is not None:
        cmd += ["--input", "-"]
    proc = subprocess.run(
        cmd,
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout
    if not out.startswith("HTTP/"):
        raise PreconditionError(f"gh api {method} {path}: no HTTP response ({proc.stderr.strip()})")
    sep = "\r\n\r\n" if "\r\n\r\n" in out else "\n\n"
    head, _, payload = out.partition(sep)
    try:
        status = int(head.split()[1])
    except (IndexError, ValueError) as exc:
        raise PreconditionError(f"gh api {method} {path}: unreadable status line") from exc
    payload = payload.strip()
    try:
        return status, (json.loads(payload) if payload else None)
    except json.JSONDecodeError as exc:
        # A proxy or a transient 5xx can answer with HTML: that is unmeasurable, not a crash.
        raise PreconditionError(f"gh api {method} {path} answered {status} with a body that is not JSON") from exc


@dataclass
class Repo:
    slug: str
    gh: GhCall = gh_call

    def _ok(self, method: str, path: str, body: dict | None = None, ok=(200,)) -> Any:  # decoded JSON
        status, data = self.gh(method, f"repos/{self.slug}/{path}", body)
        if status not in ok:
            raise PreconditionError(f"{method} {path} answered {status}: {data}")
        return data

    def ref_sha(self, branch: str) -> str:
        status, data = self.gh("GET", f"repos/{self.slug}/git/ref/heads/{branch}", None)
        if status == 404:
            raise PreconditionError(f"branch {branch} does not exist on {self.slug}")
        if status != 200 or not isinstance(data, dict):
            raise PreconditionError(f"reading {branch} answered {status}")
        return data["object"]["sha"]

    def behind_by(self, trunk_sha: str, upstream_sha: str) -> int:
        data = self._ok("GET", f"compare/{trunk_sha}...{upstream_sha}")
        return int(data["ahead_by"])  # commits in upstream that the trunk lacks

    def contains(self, head: str, ancestor: str) -> bool:
        """`ancestor` is reachable from `head` (or is it)."""
        data = self._ok("GET", f"compare/{ancestor}...{head}")
        status = data["status"]
        if status not in ("ahead", "identical", "behind", "diverged"):
            raise PreconditionError(f"compare {ancestor[:12]}...{head[:12]} answered status {status!r}")
        return status in ("ahead", "identical")

    def sync_refs(self, trunk: str, trunk_sha: str, required: list[str]) -> list[SyncRef]:
        # GitHub matches by prefix, which also returns this trunk's conflict
        # branches; _is_sync_ref keeps only the plain sync branches.
        # GitHub's matching-refs endpoint takes no page parameter and returns the whole namespace in one
        # answer (a page query returns the same array again), so it is read once. A plain sync sits among
        # closed conflict refs in that answer, which is why the match is by name below, not by position.
        refs = self._ok("GET", f"git/matching-refs/heads/{SYNC_PREFIX}--{trunk}--") or []
        found = []
        for item in refs:
            name = item["ref"].removeprefix("refs/heads/")
            if not _is_sync_ref(name, trunk):
                continue
            sha = item["object"]["sha"]
            commit = self._ok("GET", f"commits/{sha}")
            committed_at = datetime.fromisoformat(commit["commit"]["committer"]["date"].replace("Z", "+00:00"))
            parents = [p["sha"] for p in commit["parents"]]
            upstream_prefix = name.rsplit("--", 1)[1]
            is_sync_merge = len(parents) == 2 and parents[0] == trunk_sha and parents[1].startswith(upstream_prefix)
            found.append(
                SyncRef(
                    name,
                    sha,
                    is_sync_merge,
                    self.check_conclusions(sha, required),
                    committed_at,
                )
            )
        return found

    def check_runs(self, sha: str) -> list[dict]:
        """Every check run on `sha`. A full CI run posts several hundred (701 on one
        main commit, Tue 13 Oct 2026), so a required check can sit on page 3."""
        runs: list[dict] = []
        for page in range(1, CHECK_RUN_PAGE_CAP + 1):
            data = self._ok("GET", f"commits/{sha}/check-runs?per_page=100&page={page}")
            batch = data["check_runs"]
            runs.extend(batch)
            if len(batch) < 100 or len(runs) >= data["total_count"]:
                return runs
        raise PreconditionError(f"{sha} has more than {CHECK_RUN_PAGE_CAP * 100} check runs; not read")

    def check_conclusions(self, sha: str, required: list[str]) -> dict[str, str | None]:
        newest: dict[str, dict] = {}
        for run in self.check_runs(sha):
            name = run["name"]
            if name in required and (name not in newest or run["id"] > newest[name]["id"]):
                newest[name] = run
        return {name: (run["conclusion"] if run["status"] == "completed" else None) for name, run in newest.items()}

    def create_ref(self, name: str, sha: str) -> None:
        self._ok("POST", "git/refs", {"ref": f"refs/heads/{name}", "sha": sha}, ok=(201,))

    def delete_ref_at(
        self, name: str, expected_sha: str, trunk: str, upstream: str, *, sync_merge: bool = True
    ) -> None:
        """Delete `name` only while it still points at the sha the plan read. A commit pushed to
        the branch after planning is someone else's work, so the delete is refused. With
        `sync_merge`, the head must hold nothing unique (see `_is_sync_state`). The name alone
        proves nothing, because anyone who can push can take it. GitHub's REST API has no
        compare-and-delete, so one request remains between this read and the delete."""
        current = self._ok("GET", f"git/ref/heads/{name}")["object"]["sha"]
        if current != expected_sha:
            raise PreconditionError(
                f"`{name}` moved from {expected_sha[:12]} to {current[:12]} after planning; not deleted"
            )
        if sync_merge and not self._is_sync_state(name, expected_sha, trunk, upstream):
            raise PreconditionError(
                f"`{name}` at {expected_sha[:12]} is neither a sync merge nor a partial state of this pass; not deleted"
            )
        self._ok("DELETE", f"git/refs/heads/{name}", ok=(204,))

    def _is_sync_state(self, name: str, sha: str, trunk: str, upstream: str) -> bool:
        """Whether a sync head at `sha` holds nothing unique, so the workflow may delete it. That is a
        commit the trunk already holds (a branch created at the trunk tip, or a merge since advanced),
        a commit the upstream already holds (a fast-forward an interrupted pass never rebuilt), or the
        completed merge of the trunk tip and the upstream its name carries. Anything else is someone's
        work. Reachability, not a sha prefix, decides it: a prefix can be chosen by a determined pusher."""
        trunk_tip = self._ok("GET", f"git/ref/heads/{trunk}")["object"]["sha"]
        if self.contains(trunk_tip, sha):
            return True
        upstream_tip = self._ok("GET", f"git/ref/heads/{upstream}")["object"]["sha"]
        if self.contains(upstream_tip, sha):
            return True
        parents = [p["sha"] for p in self._ok("GET", f"commits/{sha}")["parents"]]
        # The second parent must be the upstream the name carries, reachable from the upstream tip. A prefix
        # match alone is not proof: a writer can push a commit whose sha starts with the same twelve characters.
        return (
            len(parents) == 2
            and parents[1].startswith(name.rsplit("--", 1)[1])
            and self.contains(trunk_tip, parents[0])
            and self.contains(upstream_tip, parents[1])
        )

    def merge(self, base: str, head_sha: str, message: str) -> tuple[int, str]:
        status, data = self.gh(
            "POST",
            f"repos/{self.slug}/merges",
            {"base": base, "head": head_sha, "commit_message": message},
        )
        if status == 201 and isinstance(data, dict):
            return status, data["sha"]
        if status in (204, 409):
            return status, ""
        raise PreconditionError(f"merge into {base} answered {status}: {data}")

    def with_trunk_first_parent(self, ref: str, sha: str, trunk_sha: str, upstream_sha: str, message: str) -> str:
        """Make the merge commit explicit unless it already has exactly the trunk tip and the upstream
        tip as its parents. When GitHub fast-forwarded instead, the merge endpoint records no merge:
        the result is the upstream commit itself, which for an upstream one commit ahead has a single
        parent. sync_refs accepts only the two-parent form, so such a head is rebuilt here, with the
        same tree, and the sync branch moves to it by fast-forward."""
        commit = self._ok("GET", f"commits/{sha}")
        parents = [p["sha"] for p in commit["parents"]]
        if parents == [trunk_sha, upstream_sha]:
            return sha
        if sha != upstream_sha:
            # Only a genuine fast-forward (the response is the upstream tip itself) may be rebuilt. Any other
            # result merged against a head that was not ours, e.g. a writer moved the ref after create_ref.
            # Rebuilding it would copy that writer's tree onto the trunk, so it is UNKNOWN, and the branch stays.
            raise PreconditionError(
                f"merge into {ref} answered {sha[:12]} with parents {[p[:12] for p in parents]}, not "
                f"{trunk_sha[:12]} and {upstream_sha[:12]}; not rebuilt"
            )
        made = self._ok(
            "POST",
            "git/commits",
            {"message": message, "tree": commit["commit"]["tree"]["sha"], "parents": [trunk_sha, upstream_sha]},
            ok=(201,),
        )
        self.fast_forward(ref, made["sha"])
        return made["sha"]

    def dispatch_ci(self, ref: str) -> None:
        self._ok(
            "POST",
            "actions/workflows/ci.yml/dispatches",
            {"ref": ref, "inputs": {"tier": "full"}},
            ok=(204,),
        )

    def fast_forward(self, branch: str, sha: str) -> None:
        self._ok("PATCH", f"git/refs/heads/{branch}", {"sha": sha, "force": False})

    def fast_forward_from(self, branch: str, sha: str, measured_sha: str) -> None:
        """Advance the trunk only while it still holds the tip the plan measured. force=false alone is not
        enough: a writer that fast-forwarded the trunk to the sync's upstream parent is an ancestor of the
        sync commit, so the update would succeed with the first parent left behind."""
        current = self._ok("GET", f"git/ref/heads/{branch}")["object"]["sha"]
        if current != measured_sha:
            raise PreconditionError(
                f"`{branch}` moved from {measured_sha[:12]} to {current[:12]}; not advanced to {sha[:12]}"
            )
        self.fast_forward(branch, sha)

    def ref_exists(self, name: str) -> bool:
        status, _ = self.gh("GET", f"repos/{self.slug}/git/ref/heads/{name}", None)
        if status not in (200, 404):
            raise PreconditionError(f"reading {name} answered {status}")
        return status == 200

    def ref_sha_or_none(self, name: str) -> str | None:
        status, data = self.gh("GET", f"repos/{self.slug}/git/ref/heads/{name}", None)
        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict):
            raise PreconditionError(f"reading {name} answered {status}")
        return data["object"]["sha"]

    def open_conflict_prs(self, trunk: str) -> list[int]:
        """Every open conflict PR into `trunk`, paged past the first 100 open PRs.

        A conflict PR beyond page one must still pause the trunk, or the pass opens a second one."""
        found: list[int] = []
        page = 1
        while True:
            pulls = self._ok("GET", f"pulls?state=open&base={trunk}&per_page=100&page={page}")
            found += [p["number"] for p in pulls if _is_conflict_pr(p, self.slug, trunk)]
            if len(pulls) < 100:
                return found
            page += 1

    def merged_conflict_prs(self, trunk: str) -> list[dict]:
        """Every merged conflict PR into `trunk`, however far back it is, oldest merge first.

        Pages every closed PR. Each merged head must be an ancestor of the trunk: a squash keeps
        the content but not the ancestry, and a later merge of an older branch must not hide a
        newer squash whose head is still missing. Creation order is not merge order, so the
        merges are sorted by `merged_at`, which is ISO 8601 UTC and so sorts as time."""
        merged: list[dict] = []
        page = 1
        while True:
            pulls = self._ok(
                "GET", f"pulls?state=closed&base={trunk}&sort=created&direction=desc&per_page=100&page={page}"
            )
            merged += [p for p in pulls if _is_conflict_pr(p, self.slug, trunk) and p.get("merged_at")]
            if len(pulls) < 100:
                return sorted(merged, key=lambda p: p["merged_at"])
            page += 1

    def open_pr(self, head: str, base: str, title: str, body: str) -> tuple[int, object]:
        return self.gh("POST", f"repos/{self.slug}/pulls", {"head": head, "base": base, "title": title, "body": body})


def _is_conflict_pr(pull: dict, slug: str, trunk: str) -> bool:
    """A conflict PR this job opened: its head branch lives in this repository and has the
    exact name the job gives it. A fork can open a PR from a branch with the same name, and
    GitHub reports only the unqualified branch name, so the repository is checked too."""
    head = pull.get("head") or {}
    repo = head.get("repo") or {}
    ref = head.get("ref") or ""
    pattern = rf"{SYNC_PREFIX}--{re.escape(trunk)}--conflict--[0-9a-f]{{12}}(?:-\d+)?"
    return repo.get("full_name") == slug and re.fullmatch(pattern, ref) is not None


def conflicted_paths(trunk_sha: str, upstream_sha: str) -> list[str] | None:
    """Paths a merge would conflict on, from the local clone; None when unmeasurable."""
    git = _resolve("git")
    gh = _resolve("gh")
    if git is None or gh is None:
        return None
    # The API picked these tips after the checkout, so either may be absent from the clone.
    # Fetch the exact objects by SHA. The checkout persists no credentials, so the fetch
    # authenticates through gh's credential helper, which reads GH_TOKEN for this one process.
    # A fetch that fails leaves the paths unmeasured.
    fetched = subprocess.run(
        [
            git,
            "-c",
            "credential.helper=",
            "-c",
            f'credential.helper=!"{gh}" auth git-credential',
            "fetch",
            "--no-tags",
            "--quiet",
            "origin",
            trunk_sha,
            upstream_sha,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if fetched.returncode != 0:
        return None
    proc = subprocess.run(
        [git, "merge-tree", "--write-tree", "--name-only", "--no-messages", trunk_sha, upstream_sha],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        return None
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return lines[1:]  # the first line is the tree id


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
