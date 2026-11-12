"""Shared helpers for the test_integration_sync test modules (split from test_integration_sync.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import integration_sync as sync

TRUNK_TIP = "a" * 40
UPSTREAM_TIP = "b" * 40
MERGE_SHA = "c" * 40
MOVED_TIP = "e" * 40
REQUIRED = ["ci gate"]


def _plan(**overrides):
    args = dict(
        trunk="main-v2",
        trunk_sha=TRUNK_TIP,
        upstream_sha=UPSTREAM_TIP,
        behind_by=5,
        sync_refs=[],
        required=REQUIRED,
        auto_ff=True,
    )
    args.update(overrides)
    return sync.plan_trunk(**args)


def _ref(checks, on_trunk_tip=True, upstream=UPSTREAM_TIP, age=timedelta(minutes=5)):
    name = sync.sync_ref_name("main-v2", upstream)
    return sync.SyncRef(name, MERGE_SHA, on_trunk_tip, checks, datetime.now(UTC) - age)


def _kinds(actions):
    return [a.kind for a in actions]


# ---------------------------------------------------------------------------
# plan_trunk: the pure decision


class FakeGitHub:
    """The REST endpoints integration_sync calls, answering like GitHub does."""

    def __init__(
        self,
        refs: dict[str, str],
        *,
        behind: int = 3,
        merge_status: int = 201,
        pr_status: int = 201,
        dispatch_status: int = 204,
    ) -> None:
        self.pulls: list[dict] = []
        self.closed_pulls: list[dict] = []
        self.pr_status = pr_status
        self.dispatch_status = dispatch_status
        self.refs = dict(refs)
        self.behind = behind
        self.merge_status = merge_status
        self.fast_forward_merges = False
        # sha -> parent shas. Shas with no entry are roots.
        self.commits: dict[str, list[str]] = {}
        self.dates: dict[str, str] = {}
        self.merges = 0
        self.checks: dict[str, list[dict]] = {}
        self.writes: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, body: dict | None) -> tuple[int, object]:
        path = path.removeprefix("repos/o/r/")
        if method != "GET":
            self.writes.append((method, path, body))
        routes = [
            ("GET", "git/ref/heads/", self._get_ref),
            ("GET", "compare/", self._compare),
            ("GET", "git/matching-refs/heads/", self._matching_refs),
            ("GET", "commits/", self._commit),
            ("POST", "git/refs", self._create_ref),
            ("POST", "git/commits", self._create_commit),
            ("DELETE", "git/refs/heads/", self._delete_ref),
            ("POST", "merges", self._merge),
            ("POST", "actions/workflows/ci.yml/dispatches", lambda _p, _b: (self.dispatch_status, None)),
            ("PATCH", "git/refs/heads/", self._update_ref),
            ("GET", "pulls?state=open&base=", self._open_pulls),
            ("GET", "pulls?state=closed&base=", self._closed_pulls),
            ("POST", "pulls", self._create_pull),
        ]
        for verb, prefix, handler in routes:
            if method == verb and path.startswith(prefix):
                return handler(path.removeprefix(prefix), body)
        raise AssertionError(f"unexpected call {method} {path}")

    def ancestors(self, sha: str) -> set[str]:
        seen, todo = set(), [sha]
        while todo:
            cur = todo.pop()
            if cur not in seen:
                seen.add(cur)
                todo.extend(self.commits.get(cur, []))
        return seen

    def _compare(self, rest: str, _body: dict | None) -> tuple[int, object]:
        base, _, head = rest.partition("...")
        base_in_head, head_in_base = base in self.ancestors(head), head in self.ancestors(base)
        if base == head:
            status = "identical"
        elif base_in_head:
            status = "ahead"
        else:
            status = "behind" if head_in_base else "diverged"
        return 200, {"status": status, "ahead_by": 0 if head_in_base else self.behind}

    def _get_ref(self, name: str, _body: dict | None) -> tuple[int, object]:
        if name not in self.refs:
            return 404, {"message": "Not Found"}
        return 200, {"object": {"sha": self.refs[name]}}

    def _matching_refs(self, rest: str, _body: dict | None) -> tuple[int, object]:
        """The whole namespace in one answer, as GitHub serves it: a page parameter changes nothing."""
        prefix = rest.partition("?")[0]
        matches = [
            {"ref": f"refs/heads/{n}", "object": {"sha": s}} for n, s in self.refs.items() if n.startswith(prefix)
        ]
        return 200, matches

    def _commit(self, rest: str, _body: dict | None) -> tuple[int, object]:
        sha, _, tail = rest.partition("/")
        if tail == "check-runs?per_page=100&page=1":
            runs = self.checks.get(sha, [])
            return 200, {"total_count": len(runs), "check_runs": runs}
        date = self.dates.get(sha, datetime.now(UTC).isoformat().replace("+00:00", "Z"))
        tree = {"sha": f"tree-{sha[:8]}"}
        return 200, {
            "parents": [{"sha": p} for p in self.commits[sha]],
            "commit": {"committer": {"date": date}, "tree": tree},
        }

    def _create_ref(self, _rest: str, body: dict | None) -> tuple[int, object]:
        body = _need(body)
        self.refs[body["ref"].removeprefix("refs/heads/")] = body["sha"]
        return 201, {}

    def _delete_ref(self, name: str, _body: dict | None) -> tuple[int, object]:
        self.refs.pop(name)
        return 204, None

    def _merge(self, _rest: str, body: dict | None) -> tuple[int, object]:
        body = _need(body)
        if self.merge_status != 201:
            return self.merge_status, None
        if getattr(self, "fast_forward_merges", False):
            # GitHub's behavior when the base is an ancestor of the head: no merge commit, the ref moves.
            self.refs[body["base"]] = body["head"]
            return 201, {"sha": body["head"]}
        sha = MERGE_SHA if self.merges == 0 else f"{self.merges:02d}" * 20
        self.merges += 1
        self.commits[sha] = [self.refs[body["base"]], body["head"]]
        self.refs[body["base"]] = sha
        return 201, {"sha": sha}

    def _create_commit(self, _rest: str, body: dict | None) -> tuple[int, object]:
        body = _need(body)
        made = sum(1 for k in self.commits if k.startswith("c")) + 1
        sha = f"c{made:039d}"
        self.commits[sha] = list(body["parents"])
        return 201, {"sha": sha}

    def _open_pulls(self, rest: str, _body: dict | None) -> tuple[int, object]:
        """Open PRs are served 100 per page like GitHub, so a lookup that stops at page one misses the rest."""
        base = rest.split("&", maxsplit=1)[0]
        assert "&page=" in rest, "open PRs are paged, or a conflict PR past the first 100 is missed"
        page = int(rest.rpartition("&page=")[2])
        mine = [p for p in self.pulls if p["base"]["ref"] == base]
        return 200, mine[(page - 1) * 100 : page * 100]

    def _closed_pulls(self, rest: str, _body: dict | None) -> tuple[int, object]:
        """`closed_pulls` is newest-created first, served 100 per page like GitHub."""
        base = rest.split("&", maxsplit=1)[0]
        assert "sort=created&direction=desc" in rest, "newest-created first, or paging misses the newest"
        page = int(rest.rpartition("&page=")[2])
        mine = [p for p in self.closed_pulls if p["base"]["ref"] == base]
        return 200, mine[(page - 1) * 100 : page * 100]

    def _create_pull(self, _rest: str, body: dict | None) -> tuple[int, object]:
        body = _need(body)
        if self.pr_status != 201:
            return self.pr_status, {"message": "GitHub Actions is not permitted to create pull requests."}
        pr = {"number": 900 + len(self.pulls), "head": {"ref": body["head"]}, "base": {"ref": body["base"]}}
        self.pulls.append(pr)
        return 201, pr

    def _update_ref(self, name: str, body: dict | None) -> tuple[int, object]:
        body = _need(body)
        assert body["force"] is False, "a trunk is only ever fast-forwarded"
        # GitHub refuses a non-fast-forward update with 422 when force is false.
        if not _descends(self.commits, body["sha"], self.refs.get(name)):
            return 422, {"message": "Update is not a fast-forward"}
        self.refs[name] = body["sha"]
        return 200, {}


def _descends(commits: dict[str, list[str]], sha: str, ancestor: str | None) -> bool:
    """True when `ancestor` is `sha` or one of its ancestors. A commit with no entry is a root."""
    frontier, seen = [sha], set()
    while frontier:
        current = frontier.pop()
        if current == ancestor:
            return True
        if current not in seen:
            seen.add(current)
            frontier.extend(commits.get(current, []))
    return False


def _need(body: dict | None) -> dict:
    """A write route is always called with a JSON body; fail loudly if one is missing."""
    assert body is not None, "write route called without a body"
    return body


def _is_plain_sync(name: str) -> bool:
    return name.startswith("sync--") and "--conflict--" not in name


def _run(fake: FakeGitHub, *, auto_ff: bool = True, dry_run: bool = False, advance_only: bool = False):
    repo = sync.Repo("o/r", gh=fake)
    return sync.run_trunk(repo, "main-v2", "main", REQUIRED, auto_ff, dry_run, advance_only=advance_only)


def _green(fake: FakeGitHub, sha: str) -> None:
    fake.checks[sha] = [{"id": 1, "name": "ci gate", "status": "completed", "conclusion": "success"}]


def _merged_conflict_pr(head_sha: str) -> dict:
    return {
        "number": 41,
        "head": {"ref": "sync--main-v2--conflict--" + head_sha[:12], "sha": head_sha, "repo": {"full_name": "o/r"}},
        "base": {"ref": "main-v2"},
        "merged_at": "2026-10-14T10:00:00Z",
    }


def _closed_feature_prs(n: int) -> list[dict]:
    return [
        {"number": 1000 + i, "head": {"ref": f"af--f{i}", "sha": "9" * 40}, "base": {"ref": "main-v2"}}
        for i in range(n)
    ]


def _two_trunk_config(tmp_path: Path) -> Path:
    config = tmp_path / "trunks.json"
    trunks = [{"branch": "main-v9", "upstream": "main"}, {"branch": "main-v2", "upstream": "main"}]
    config.write_text(json.dumps({"required_checks": REQUIRED, "trunks": trunks}))
    return config


def _main_with_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payloads: dict[str, object]
) -> tuple[int, str, FakeGitHub]:
    """Run main() where the first endpoint containing each key answers with its malformed payload."""
    monkeypatch.delenv("INTEGRATION_SYNC_WRITES", raising=False)
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})

    def gh(method: str, path: str, body: dict | None) -> tuple[int, object]:
        for marker, payload in payloads.items():
            if marker in path:
                return 200, payload
        return fake(method, path, body)

    code = sync.main(["--config", str(_two_trunk_config(tmp_path))], repo=sync.Repo("o/r", gh=gh))
    return code, capsys.readouterr().out, fake
