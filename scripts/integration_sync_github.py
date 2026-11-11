"""GitHub boundary for the integration sync: `gh api` calls, the Repo reads and writes, local git.

Split from ``scripts/integration_sync.py`` (#10976). Import the names from
``scripts.integration_sync``; this module is an implementation slice.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from scripts.integration_sync_plan import SYNC_PREFIX, PreconditionError, SyncRef, _is_sync_ref

# Pages of 100 check runs read per commit before giving up as UNKNOWN.
CHECK_RUN_PAGE_CAP = 20

# ---------------------------------------------------------------------------
# GitHub boundary


GhCall = Callable[[str, str, dict | None], tuple[int, object]]


def _resolve(name: str) -> str | None:
    """The absolute path of an executable, looked up once; the call then runs that path,
    so a PATH change between steps cannot swap in a different binary."""
    return shutil.which(name)


def _resolve_seen_by_the_facade(name: str) -> str | None:
    """`_resolve` as bound on scripts.integration_sync, so a monkeypatch there still reaches gh_call and
    conflicted_paths after the #10976 split. Imported late: the facade imports this module."""
    from scripts import integration_sync

    return integration_sync._resolve(name)


def gh_call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    """`gh api` with the HTTP status kept, so 409 and 204 are answers, not crashes."""
    gh = _resolve_seen_by_the_facade("gh")
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
    git = _resolve_seen_by_the_facade("git")
    gh = _resolve_seen_by_the_facade("gh")
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
