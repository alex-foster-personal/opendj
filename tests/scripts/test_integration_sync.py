"""Regression contract for scripts/integration_sync.py (ADR-NEW-post-v1-branch-strategy).

[if] a version trunk already contains its upstream tip [then] nothing is created [else stop].
[if] a trunk is behind and no sync branch is pending [then] one is created at the trunk
tip, the upstream tip is merged into it, and CI is dispatched on it [else stop].
[if] the merge conflicts [then] the sync branch is deleted and the run exits 1 [else stop].
[if] auto fast-forward is on and every required check is PRESENT with success [then] the
trunk advances to the sync commit [else stop].
[if] a required check has no run at all, or is still running [then] the trunk never
advances [else stop].
[if] the trunk moved while the current sync was pending [then] the new trunk tip is
merged into the sync branch, CI runs again, and the sync still lands [else stop].
[if] the trunk moved past a sync branch for an older upstream tip [then] that branch is
deleted [else stop].
[if] a sync commit has no required-check run long after it was made [then] its branch is
deleted and the run exits 1 [else stop].
[if] the newest merged conflict PR is not in the trunk's history (squash) [then] the run
exits 1 and writes nothing for that trunk [else stop].
[if] one trunk cannot be measured [then] the others are still synced, and the run exits
10 [else stop].
[if] INTEGRATION_SYNC_WRITES is not true [then] the run writes nothing [else stop].
[if] a required check failed at the current upstream tip [then] the run exits 1 and does
not start another sync of the same tip [else stop].
[if] a configured branch does not exist [then] the run exits 10 (UNKNOWN), never 0
[else stop].

The GitHub boundary is the `gh` callable on `Repo`; the tests replace it with a small
in-memory model of the REST endpoints the script calls, so every write the script
makes is recorded and asserted.

-Claude
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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


def test_up_to_date_trunk_creates_nothing() -> None:
    assert _kinds(_plan(behind_by=0)) == ["up-to-date"]


def test_behind_trunk_with_no_sync_branch_starts_one() -> None:
    actions = _plan()
    assert _kinds(actions) == ["create"]
    assert actions[0].ref == "sync--main-v2--" + UPSTREAM_TIP[:12]


def test_green_sync_branch_advances_when_auto_ff_is_on() -> None:
    actions = _plan(sync_refs=[_ref({"ci gate": "success"})])
    assert _kinds(actions) == ["advance", "delete"]
    assert actions[0].sha == MERGE_SHA


def test_green_sync_branch_waits_when_auto_ff_is_off() -> None:
    assert _kinds(_plan(sync_refs=[_ref({"ci gate": "success"})], auto_ff=False)) == ["wait"]


def test_missing_required_check_is_pending_not_green() -> None:
    """Absence of failure is not presence of success."""
    assert _kinds(_plan(sync_refs=[_ref({})])) == ["wait"]


def test_running_required_check_is_pending() -> None:
    assert _kinds(_plan(sync_refs=[_ref({"ci gate": None})])) == ["wait"]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "neutral", "timed_out"])
def test_any_non_success_conclusion_is_red_not_green(conclusion: str) -> None:
    actions = _plan(sync_refs=[_ref({"ci gate": conclusion})])
    assert "advance" not in _kinds(actions)
    assert _kinds(actions) == ["red"]


def test_the_red_note_names_each_failed_required_check_and_its_conclusion() -> None:
    """Codex P2 on #5728: with two required checks, the red report must say which one failed."""
    actions = _plan(
        sync_refs=[_ref({"ci gate": "success", "reqs-check": "failure"})],
        required=["ci gate", "reqs-check"],
    )
    red = next(a for a in actions if a.kind == "red")
    assert red.note == "required check failed at this upstream tip: reqs-check failure"


def test_red_at_an_older_upstream_tip_is_replaced_by_a_new_sync() -> None:
    old = _ref({"ci gate": "failure"}, upstream="d" * 40)
    assert _kinds(_plan(sync_refs=[old])) == ["delete", "create"]


def test_the_current_sync_is_refreshed_when_the_trunk_moved_past_it() -> None:
    stale = _ref({"ci gate": "success"}, on_trunk_tip=False)
    actions = _plan(sync_refs=[stale])
    assert _kinds(actions) == ["refresh"], "a green but stale merge must never advance"
    assert actions[0].ref == stale.name


def test_a_stale_sync_for_an_older_upstream_tip_is_deleted() -> None:
    stale = _ref({"ci gate": "success"}, on_trunk_tip=False, upstream="d" * 40)
    assert _kinds(_plan(sync_refs=[stale])) == ["delete", "create"]


def test_a_sync_whose_ci_never_started_is_given_up_after_the_cap() -> None:
    old = _ref({}, age=timedelta(hours=sync.STUCK_HOURS + 1))
    # The stuck branch is deleted, and the same pass starts its replacement rather than waiting a day.
    assert _kinds(_plan(sync_refs=[old])) == ["stuck", "create"]


def test_a_young_sync_with_no_ci_run_yet_waits() -> None:
    """The control for the cap: a just-dispatched run has no check yet and must wait."""
    young = _ref({}, age=timedelta(hours=sync.STUCK_HOURS - 1))
    assert _kinds(_plan(sync_refs=[young])) == ["wait"]


def test_a_running_check_is_never_stuck_however_old() -> None:
    old = _ref({"ci gate": None}, age=timedelta(hours=sync.STUCK_HOURS + 1))
    assert _kinds(_plan(sync_refs=[old])) == ["wait"]


def test_advance_only_never_starts_a_sync() -> None:
    assert _kinds(_plan(advance_only=True)) == ["idle"]
    assert _kinds(_plan(advance_only=True, sync_refs=[_ref({"ci gate": "success"})])) == ["advance", "delete"]


def test_no_required_checks_is_a_precondition_error() -> None:
    with pytest.raises(sync.PreconditionError):
        _plan(sync_refs=[_ref({"ci gate": "success"})], required=[])


# ---------------------------------------------------------------------------
# run_trunk against an in-memory REST model


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


def test_a_behind_trunk_gets_a_merged_sync_branch_and_a_ci_dispatch() -> None:
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    code, _ = _run(fake)
    name = "sync--main-v2--" + UPSTREAM_TIP[:12]
    assert code == sync.EXIT_OK
    assert fake.refs[name] == MERGE_SHA
    assert fake.refs["main-v2"] == TRUNK_TIP, "the trunk must not move before CI is green"
    assert ("POST", "actions/workflows/ci.yml/dispatches", {"ref": name, "inputs": {"tier": "full"}}) in fake.writes


def test_then_a_green_run_fast_forwards_the_trunk() -> None:
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    _run(fake)
    fake.checks[MERGE_SHA] = [{"id": 1, "name": "ci gate", "status": "completed", "conclusion": "success"}]
    code, _ = _run(fake)
    assert code == sync.EXIT_OK
    assert fake.refs["main-v2"] == MERGE_SHA
    assert not [n for n in fake.refs if _is_plain_sync(n)]


def test_the_newest_check_run_decides_after_a_rerun() -> None:
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    _run(fake)
    fake.checks[MERGE_SHA] = [
        {"id": 2, "name": "ci gate", "status": "completed", "conclusion": "failure"},
        {"id": 1, "name": "ci gate", "status": "completed", "conclusion": "success"},
    ]
    code, _ = _run(fake)
    assert code == sync.EXIT_NEEDS_HUMAN
    assert fake.refs["main-v2"] == TRUNK_TIP


def test_a_conflict_opens_a_pr_from_the_upstream_tip_and_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "conflicted_paths", lambda *_: ["justfile"])
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP}, merge_status=409)
    code, lines = _run(fake)
    conflict = "sync--main-v2--conflict--" + UPSTREAM_TIP[:12]
    assert code == sync.EXIT_NEEDS_HUMAN
    assert not [n for n in fake.refs if _is_plain_sync(n)]
    assert fake.refs[conflict] == UPSTREAM_TIP, "the conflict branch starts at the upstream tip, untouched"
    assert [(p["head"]["ref"], p["base"]["ref"]) for p in fake.pulls] == [(conflict, "main-v2")]
    assert fake.refs["main-v2"] == TRUNK_TIP
    assert "justfile" in "".join(lines)
    assert "opened PR #900" in "".join(lines)


def test_an_unmeasurable_conflict_opens_no_pr_and_reports_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sol P1 on #5728: a 409 whose conflicted paths cannot be measured is UNKNOWN, not a conflict
    PR that names no paths. The next pass retries the merge."""
    monkeypatch.setenv("INTEGRATION_SYNC_WRITES", "true")
    monkeypatch.setattr(sync, "conflicted_paths", lambda *_: None)
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP}, merge_status=409)
    config = tmp_path / "trunks.json"
    config.write_text(json.dumps({"required_checks": REQUIRED, "trunks": [{"branch": "main-v2", "upstream": "main"}]}))
    code = sync.main(["--config", str(config)], repo=sync.Repo("o/r", gh=fake))
    assert code == sync.EXIT_PRECONDITION
    assert fake.pulls == []
    assert not [n for n in fake.refs if "--conflict--" in n]
    # Pin the message, not just UNKNOWN: a TypeError from an unmeasured None is also caught in main().
    assert "could not be measured" in capsys.readouterr().out


def test_a_sync_branch_that_is_not_the_merge_of_its_upstream_is_never_advanced() -> None:
    """Sol P1 on #5728: a green sync branch whose head is not a merge of the trunk and the upstream
    its name carries must not fast-forward the trunk, even with the right first parent."""
    forged = "f" * 40
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.commits[forged] = [TRUNK_TIP, "9" * 40]
    fake.refs["sync--main-v2--" + "e" * 12] = forged
    _green(fake, forged)
    _run(fake)
    assert fake.refs["main-v2"] == TRUNK_TIP, "a branch that is not the upstream's merge advanced the trunk"


def test_a_sync_branch_that_moved_after_planning_or_is_not_a_merge_is_never_deleted() -> None:
    """Sol P1 on #5728: a delete planned against one sha must not destroy a commit pushed to the
    branch since. Positive control first: the same delete goes through when the sha still matches."""
    name = "sync--main-v2--" + UPSTREAM_TIP[:12]
    planned, pushed, forged, alien = "5" * 40, "6" * 40, "f" * 40, "7" * 40
    other = "sync--main-v2--" + "e" * 12
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP, name: pushed, other: forged})
    fake.commits[planned] = [TRUNK_TIP, UPSTREAM_TIP]
    fake.commits[pushed] = [planned, "8" * 40]
    fake.commits[forged] = [TRUNK_TIP, "9" * 40]
    repo = sync.Repo("o/r", gh=fake)

    with pytest.raises(sync.PreconditionError):
        repo.delete_ref_at(name, planned, "main-v2", "main")
    assert fake.refs[name] == pushed, "a commit pushed after planning was deleted"

    with pytest.raises(sync.PreconditionError):
        repo.delete_ref_at(other, forged, "main-v2", "main")
    assert fake.refs[other] == forged, "a branch that is not the upstream's merge was deleted"

    # Sol P1 on #5728: the second parent matches the upstream, but the first is not on the trunk.
    # Same name as the real sync branch, so the second parent is the upstream's and only the first is foreign.
    fake.commits[alien] = ["d" * 40, UPSTREAM_TIP]
    fake.refs[name] = alien
    with pytest.raises(sync.PreconditionError):
        repo.delete_ref_at(name, alien, "main-v2", "main")
    assert fake.refs[name] == alien, "a foreign first parent with the upstream second parent was deleted"

    fake.refs[name] = planned
    repo.delete_ref_at(name, planned, "main-v2", "main")
    assert name not in fake.refs, "positive control: the planned sync merge should be deleted"


def test_a_merge_answered_against_a_moved_ref_is_unknown_and_never_rebuilt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex P2 on #5728: a writer moves the sync ref between create_ref and the merge, so the merge
    has the writer's head as first parent. That result must be UNKNOWN; rebuilding it would copy the
    writer's tree onto the trunk."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    writer = "d" * 40
    merge = fake._merge

    def moved_then_merged(rest: str, body: dict | None) -> tuple[int, object]:
        assert body is not None
        fake.refs[body["base"]] = writer
        return merge(rest, body)

    monkeypatch.setattr(fake, "_merge", moved_then_merged)
    with pytest.raises(sync.PreconditionError):
        _run(fake)
    assert fake.refs["main-v2"] == TRUNK_TIP, "a merge against a writer's head moved the trunk"
    assert not [w for w in fake.writes if w[1] == "git/commits"], "the writer's tree was rebuilt onto the trunk"


def test_an_interrupted_pass_partial_head_is_deleted_and_a_pushed_commit_is_not() -> None:
    """Codex P2 on #5728: a pass interrupted after create_ref or after a fast-forward leaves the
    sync ref at a commit the trunk or the upstream already holds. Nothing unique is lost by deleting
    it, so it must be deletable; a commit neither holds is a pushed one and stays."""
    name = "sync--main-v2--" + UPSTREAM_TIP[:12]
    pushed = "6" * 40
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.commits[pushed] = [TRUNK_TIP, "8" * 40]
    repo = sync.Repo("o/r", gh=fake)

    fake.refs[name] = TRUNK_TIP  # created at the trunk tip, never merged
    repo.delete_ref_at(name, TRUNK_TIP, "main-v2", "main")
    assert name not in fake.refs, "a partial create at the trunk tip was not deletable"

    fake.refs[name] = UPSTREAM_TIP  # fast-forwarded to the upstream commit, never rebuilt
    repo.delete_ref_at(name, UPSTREAM_TIP, "main-v2", "main")
    assert name not in fake.refs, "a partial fast-forward to the upstream tip was not deletable"

    fake.refs[name] = pushed
    with pytest.raises(sync.PreconditionError):
        repo.delete_ref_at(name, pushed, "main-v2", "main")
    assert fake.refs[name] == pushed, "a pushed commit neither branch holds was deleted"


def test_a_trunk_that_already_holds_the_upstream_tip_drops_its_pending_sync_instead_of_rebuilding() -> None:
    """Codex P2 on #5728: when feature work moves the trunk to a commit that already contains the
    upstream, behind_by is 0. The pending sync is then not needed, so it must be dropped as an
    up-to-date no-op, not refreshed into a redundant CI run."""
    current = "sync--main-v2--" + UPSTREAM_TIP[:12]
    pending = sync.SyncRef(current, "7" * 40, False, {}, datetime.now(UTC))
    actions = sync.plan_trunk(
        trunk="main-v2",
        trunk_sha="9" * 40,
        upstream_sha=UPSTREAM_TIP,
        behind_by=0,
        sync_refs=[pending],
        required=REQUIRED,
        auto_ff=False,
    )
    assert [a.kind for a in actions] == ["delete", "up-to-date"], "a no-op trunk rebuilt its pending sync"
    assert actions[0].ref == current


def test_a_one_commit_fast_forward_is_rebuilt_as_a_two_parent_merge() -> None:
    """Sol P1 on #5728: when the upstream is exactly one commit ahead, GitHub fast-forwards the sync
    ref to the one-parent upstream commit. That head must be rebuilt as the two-parent merge that
    sync_refs accepts, or every pass rebuilds it and the trunk never advances."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.fast_forward_merges = True
    fake.commits[UPSTREAM_TIP] = [TRUNK_TIP]
    code, _ = _run(fake, auto_ff=False)
    assert code == sync.EXIT_OK
    head = fake.refs["sync--main-v2--" + UPSTREAM_TIP[:12]]
    assert fake.commits[head] == [TRUNK_TIP, UPSTREAM_TIP], "the sync head is not the two-parent merge"


def test_a_trunk_advance_survives_a_later_failure_in_the_same_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sol P1 on #5728: a fast-forward that lands before a later step raises must still start the
    downstream trunk's sync in the same pass. The advance is recorded when it happens, not when
    the trunk's pass returns."""
    monkeypatch.setenv("INTEGRATION_SYNC_WRITES", "true")
    monkeypatch.setenv("INTEGRATION_SYNC_AUTO_FF", "true")
    older_merge = "d" * 40
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP, "main-v3": "9" * 40})
    fake.commits[older_merge] = [TRUNK_TIP, "e" * 40]
    fake.refs["sync--main-v2--" + "e" * 12] = older_merge
    _green(fake, older_merge)
    real_behind_by = sync.Repo.behind_by

    def fails_after_the_advance(self, trunk_sha, upstream_sha):  # type: ignore[no-untyped-def]
        # Only the re-plan on the advanced tip reads behind_by from older_merge.
        if trunk_sha == older_merge:
            raise sync.PreconditionError("re-plan read failed after the advance")
        return real_behind_by(self, trunk_sha, upstream_sha)

    monkeypatch.setattr(sync.Repo, "behind_by", fails_after_the_advance)
    config = tmp_path / "trunks.json"
    trunks = [{"branch": "main-v2", "upstream": "main"}, {"branch": "main-v3", "upstream": "main-v2"}]
    config.write_text(json.dumps({"required_checks": REQUIRED, "trunks": trunks}))
    code = sync.main(["--config", str(config), "--advance-only"], repo=sync.Repo("o/r", gh=fake))
    assert code == sync.EXIT_PRECONDITION
    assert fake.refs["main-v2"] == older_merge
    assert any(name.startswith("sync--main-v3--") for name in fake.refs), "the advance was lost with the failure"


def test_a_409_that_measures_no_conflicted_paths_opens_no_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sol P1 on #5728: a 409 whose merge-tree lists no paths is an unmeasured conflict, not one
    with 'none listed'. It is UNKNOWN, and the next pass retries it."""
    monkeypatch.setenv("INTEGRATION_SYNC_WRITES", "true")
    monkeypatch.setattr(sync, "conflicted_paths", lambda *_: [])
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP}, merge_status=409)
    config = tmp_path / "trunks.json"
    config.write_text(json.dumps({"required_checks": REQUIRED, "trunks": [{"branch": "main-v2", "upstream": "main"}]}))
    code = sync.main(["--config", str(config)], repo=sync.Repo("o/r", gh=fake))
    assert code == sync.EXIT_PRECONDITION
    assert fake.pulls == []
    assert "could not be measured" in capsys.readouterr().out


def test_an_open_conflict_pr_pauses_the_trunk() -> None:
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.pulls.append(
        {
            "number": 7,
            "head": {"ref": "sync--main-v2--conflict--" + "d" * 12, "repo": {"full_name": "o/r"}},
            "base": {"ref": "main-v2"},
        }
    )
    code, lines = _run(fake)
    assert code == sync.EXIT_OK
    assert fake.writes == [], "no new sync while a conflict PR is open"
    assert "#7" in "".join(lines)


def test_an_open_conflict_pr_past_the_first_page_still_pauses_the_trunk() -> None:
    """Codex P2 on #5728: a conflict PR behind 100 other open PRs must still pause the trunk."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.pulls = [{"number": 1000 + i, "head": {"ref": f"af--o{i}"}, "base": {"ref": "main-v2"}} for i in range(150)]
    fake.pulls.append(
        {
            "number": 7,
            "head": {"ref": "sync--main-v2--conflict--" + "d" * 12, "repo": {"full_name": "o/r"}},
            "base": {"ref": "main-v2"},
        }
    )
    code, lines = _run(fake)
    assert code == sync.EXIT_OK
    assert fake.writes == [], "no new sync while a conflict PR is open, however far down the list"
    assert "#7" in "".join(lines)


def test_a_closed_conflict_branch_with_a_resolvers_commits_is_never_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex P2 on #5728: a stale conflict branch from a closed, unmerged PR must not carry into the new PR."""
    monkeypatch.setattr(sync, "conflicted_paths", lambda *_: ["justfile"])
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP}, merge_status=409)
    stale = "sync--main-v2--conflict--" + UPSTREAM_TIP[:12]
    fake.refs[stale] = "a" * 40
    code, _ = _run(fake)
    fresh = stale + "-2"
    assert code == sync.EXIT_NEEDS_HUMAN
    assert fake.refs[stale] == "a" * 40, "the old branch is left as it was"
    assert fake.refs[fresh] == UPSTREAM_TIP, "the new PR starts from the pristine upstream tip"
    assert [(p["head"]["ref"], p["base"]["ref"]) for p in fake.pulls] == [(fresh, "main-v2")]


def test_gh_is_resolved_once_and_a_missing_gh_is_unknown_not_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync.shutil, "which", lambda _name: None)
    with pytest.raises(sync.PreconditionError, match="gh is not on PATH"):
        sync.gh_call("GET", "repos/o/r/git/ref/heads/main")
    assert sync.conflicted_paths("a" * 40, "b" * 40) is None


def test_a_fork_pr_named_like_a_conflict_branch_does_not_pause_the_trunk() -> None:
    """Codex P2 on #5728: a PR from a fork can use the same branch name; only this repository's count."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.pulls.append(
        {
            "number": 8,
            "head": {"ref": "sync--main-v2--conflict--" + "d" * 12, "repo": {"full_name": "x/fork"}},
            "base": {"ref": "main-v2"},
        }
    )
    code, lines = _run(fake)
    assert code == sync.EXIT_OK
    assert fake.writes != [], "a fork's PR does not stop this repository's sync"
    assert "#8" not in "".join(lines)


def test_a_fast_forwardable_merge_is_rebuilt_as_a_two_parent_commit() -> None:
    """Codex P1 on #5728: GitHub fast-forwards when the trunk is an ancestor of upstream. The sync
    must still record the trunk tip as first parent, or it never advances and rebuilds every hour."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.fast_forward_merges = True
    fake.commits[UPSTREAM_TIP] = ["e" * 40]  # upstream two commits ahead: its parent is not the trunk tip
    code, _ = _run(fake)
    sync_ref = "sync--main-v2--" + UPSTREAM_TIP[:12]
    assert code == sync.EXIT_OK
    assert fake.commits[fake.refs[sync_ref]] == [TRUNK_TIP, UPSTREAM_TIP]


def test_a_refused_pr_still_reports_the_conflict_and_keeps_the_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "conflicted_paths", lambda *_: ["justfile"])
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP}, merge_status=409, pr_status=403)
    code, lines = _run(fake)
    assert code == sync.EXIT_NEEDS_HUMAN
    assert "could NOT open a PR (403" in "".join(lines)
    assert "sync--main-v2--conflict--" + UPSTREAM_TIP[:12] in fake.refs


def test_dry_run_writes_nothing() -> None:
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    _run(fake, dry_run=True)
    assert fake.writes == []


def test_a_sibling_trunk_with_a_longer_name_is_not_read_as_ours() -> None:
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP, "sync--main-v20--" + "f" * 12: "f" * 40})
    code, _ = _run(fake)
    assert code == sync.EXIT_OK
    assert "sync--main-v20--" + "f" * 12 in fake.refs


def test_a_missing_trunk_is_unknown_not_up_to_date(tmp_path: Path) -> None:
    config = tmp_path / "trunks.json"
    config.write_text(json.dumps({"required_checks": REQUIRED, "trunks": [{"branch": "main-v9", "upstream": "main"}]}))
    fake = FakeGitHub({"main": UPSTREAM_TIP})
    code = sync.main(["--config", str(config)], repo=sync.Repo("o/r", gh=fake))
    assert code == sync.EXIT_PRECONDITION


def test_the_committed_config_names_the_ci_gate_and_a_chain() -> None:
    required, trunks = sync.load_config(sync.DEFAULT_CONFIG)
    assert "ci gate" in required
    branches = [b for b, _ in trunks]
    for branch, upstream in trunks:
        assert upstream == "main" or branches.index(upstream) < branches.index(branch), (
            f"{branch} is synced before its upstream {upstream}"
        )


def test_a_leftover_conflict_branch_is_not_read_as_a_sync_branch() -> None:
    """A conflict branch with no open PR (closed by hand) must not block or be advanced."""
    leftover = "sync--main-v2--conflict--" + "d" * 12
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP, leftover: "d" * 40})
    code, _ = _run(fake)
    assert code == sync.EXIT_OK
    assert fake.refs["sync--main-v2--" + UPSTREAM_TIP[:12]] == MERGE_SHA
    assert leftover in fake.refs


def test_a_trunk_that_moves_between_passes_still_gets_its_sync() -> None:
    """Grok P1-1 on #5728: a busy trunk must not starve the sync."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    name = "sync--main-v2--" + UPSTREAM_TIP[:12]
    _run(fake)
    _green(fake, MERGE_SHA)
    # A feature PR lands on the trunk before the next pass.
    fake.commits[MOVED_TIP] = [TRUNK_TIP]
    fake.refs["main-v2"] = MOVED_TIP
    code, _ = _run(fake)
    refreshed = fake.refs[name]
    assert code == sync.EXIT_OK
    assert refreshed != MERGE_SHA, "the sync branch was rebuilt"
    assert fake.commits[refreshed] == [MOVED_TIP, UPSTREAM_TIP], "the new trunk tip is the FIRST parent"
    assert fake.refs["main-v2"] == MOVED_TIP, "the stale green run must not advance the trunk"
    assert fake.writes[-1] == ("POST", "actions/workflows/ci.yml/dispatches", {"ref": name, "inputs": {"tier": "full"}})
    _green(fake, refreshed)
    code, _ = _run(fake, advance_only=True)
    assert code == sync.EXIT_OK
    assert fake.refs["main-v2"] == refreshed
    assert {UPSTREAM_TIP, MOVED_TIP} <= fake.ancestors(fake.refs["main-v2"])


def test_a_rebuild_that_conflicts_opens_a_conflict_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "conflicted_paths", lambda *_: ["apps/x.py"])
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    _run(fake)
    fake.commits[MOVED_TIP] = [TRUNK_TIP]
    fake.refs["main-v2"] = MOVED_TIP
    fake.merge_status = 409
    code, lines = _run(fake)
    assert code == sync.EXIT_NEEDS_HUMAN
    assert not [n for n in fake.refs if _is_plain_sync(n)]
    assert fake.refs["main-v2"] == MOVED_TIP
    assert [p["head"]["ref"] for p in fake.pulls] == ["sync--main-v2--conflict--" + UPSTREAM_TIP[:12]]
    assert "apps/x.py" in "".join(lines)


def test_a_green_sync_with_the_trunk_only_as_second_parent_never_advances() -> None:
    """Codex P1 on #5728: ancestry is not enough; the trunk tip must be the first parent."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    name = "sync--main-v2--" + UPSTREAM_TIP[:12]
    bent = (
        "9" * 40
    )  # distinct from UPSTREAM_TIP: a shared sha would be the upstream tip itself, a legitimate partial state
    fake.commits[bent] = [UPSTREAM_TIP, TRUNK_TIP]
    fake.refs[name] = bent
    _green(fake, bent)
    code, _ = _run(fake, advance_only=True)
    # A commit in our namespace that is not the upstream's merge is kept and reported UNKNOWN, not deleted.
    assert code == sync.EXIT_PRECONDITION
    assert fake.refs[name] == bent, "a foreign commit under a sync name was deleted"
    assert fake.refs["main-v2"] == TRUNK_TIP, "a first-parent-bending commit must never land"
    assert not [w for w in fake.writes if w[0] == "DELETE"], "a foreign branch was deleted or rebuilt over"


def test_a_sync_whose_ci_never_reported_is_deleted_and_reported_red() -> None:
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    _run(fake)
    long_ago = datetime.now(UTC) - timedelta(hours=sync.STUCK_HOURS + 1)
    fake.dates[MERGE_SHA] = long_ago.isoformat().replace("+00:00", "Z")
    code, lines = _run(fake)
    assert code == sync.EXIT_NEEDS_HUMAN
    # Codex P2 on #5728: the stuck branch is deleted and a fresh sync starts in the same pass, so the trunk is not
    # left waiting for the next daily run. The fresh sync is not the one that never reported.
    fresh = [fake.refs[n] for n in fake.refs if _is_plain_sync(n)]
    assert fresh and MERGE_SHA not in fresh
    assert "never reported" in "".join(lines)


def test_a_failed_dispatch_leaves_no_untested_sync_branch() -> None:
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP}, dispatch_status=403)
    with pytest.raises(sync.PreconditionError):
        _run(fake)
    assert not [n for n in fake.refs if _is_plain_sync(n)]


def _merged_conflict_pr(head_sha: str) -> dict:
    return {
        "number": 41,
        "head": {"ref": "sync--main-v2--conflict--" + head_sha[:12], "sha": head_sha, "repo": {"full_name": "o/r"}},
        "base": {"ref": "main-v2"},
        "merged_at": "2026-10-14T10:00:00Z",
    }


def test_a_squash_merged_conflict_pr_is_red_and_blocks_the_trunk() -> None:
    """Grok P1-2 on #5728: a squash keeps the content and loses the ancestry."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.closed_pulls.append(_merged_conflict_pr("f" * 40))
    code, lines = _run(fake)
    assert code == sync.EXIT_NEEDS_HUMAN
    assert fake.writes == []
    assert "#41" in "".join(lines)
    assert "merge commit" in "".join(lines)


def test_a_conflict_pr_merged_with_a_merge_commit_lets_the_sync_go_on() -> None:
    """The control: the same PR merged with its history must not read as squashed."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.commits[TRUNK_TIP] = ["f" * 40]
    fake.closed_pulls.append(_merged_conflict_pr("f" * 40))
    code, _ = _run(fake)
    assert code == sync.EXIT_OK
    assert fake.refs["sync--main-v2--" + UPSTREAM_TIP[:12]] == MERGE_SHA


def _closed_feature_prs(n: int) -> list[dict]:
    return [
        {"number": 1000 + i, "head": {"ref": f"af--f{i}", "sha": "9" * 40}, "base": {"ref": "main-v2"}}
        for i in range(n)
    ]


def test_a_squash_merged_conflict_pr_past_the_first_page_still_blocks() -> None:
    """Codex P1 on #5728: newer unrelated closed PRs must not page the squash out of sight."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.closed_pulls = [*_closed_feature_prs(250), _merged_conflict_pr("f" * 40)]
    code, lines = _run(fake)
    assert code == sync.EXIT_NEEDS_HUMAN
    assert fake.writes == []
    assert "#41" in "".join(lines)


def test_a_newer_conflict_pr_merged_with_history_supersedes_an_old_squash() -> None:
    """The control for paging: the newest merged conflict PR decides, not any old one."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    # The newer merge's history holds the older squash's head, so the trunk contains both.
    fake.commits[TRUNK_TIP] = ["e" * 40]
    fake.commits["e" * 40] = ["f" * 40]
    newer = {**_merged_conflict_pr("e" * 40), "number": 42}
    fake.closed_pulls = [*_closed_feature_prs(150), newer, *_closed_feature_prs(150), _merged_conflict_pr("f" * 40)]
    code, _ = _run(fake)
    assert code == sync.EXIT_OK
    assert fake.refs["sync--main-v2--" + UPSTREAM_TIP[:12]] == MERGE_SHA


def test_the_fake_refuses_a_non_fast_forward_update_as_github_does() -> None:
    """Codex P1 on #5728: a fake that accepts what GitHub refuses proves nothing about the trunk guard."""
    fake = FakeGitHub({"main-v2": TRUNK_TIP})
    fake.commits["d" * 40] = []  # an unrelated root, not a descendant of the trunk tip
    fake.commits["c" * 40] = [TRUNK_TIP]  # a real fast-forward
    repo = sync.Repo("o/r", gh=fake)
    with pytest.raises(sync.PreconditionError):
        repo.fast_forward("main-v2", "d" * 40)
    assert fake.refs["main-v2"] == TRUNK_TIP
    repo.fast_forward("main-v2", "c" * 40)
    assert fake.refs["main-v2"] == "c" * 40


def test_an_advance_refuses_when_the_trunk_moved_after_it_was_measured() -> None:
    """Codex P2 on #5728: force=false accepts a sync whose second parent is the trunk's new tip. The advance
    must hold the tip it measured, or the trunk ends up with a first parent that was never measured."""
    fake = FakeGitHub({"main-v2": UPSTREAM_TIP})
    fake.commits[MERGE_SHA] = [TRUNK_TIP, UPSTREAM_TIP]  # a sync whose first parent is the old trunk
    fake.commits[TRUNK_TIP] = []
    repo = sync.Repo("o/r", gh=fake)
    with pytest.raises(sync.PreconditionError):
        repo.fast_forward_from("main-v2", MERGE_SHA, TRUNK_TIP)
    assert fake.refs["main-v2"] == UPSTREAM_TIP
    assert not [w for w in fake.writes if w[0] == "PATCH"]
    repo.fast_forward_from("main-v2", MERGE_SHA, UPSTREAM_TIP)
    assert fake.refs["main-v2"] == MERGE_SHA


def test_a_refused_advance_stops_the_trunk_and_keeps_its_sync_branch() -> None:
    """Codex P2 on #5728: a refused advance must not count as landed, must not let the sync branch behind it
    be deleted, and must not re-plan downstream from a tip the trunk never reached."""
    name = f"sync--main-v2--{MERGE_SHA[:12]}"
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": UPSTREAM_TIP, name: MERGE_SHA})
    fake.commits[MERGE_SHA] = [TRUNK_TIP, UPSTREAM_TIP]
    repo = sync.Repo("o/r", gh=fake)
    actions = [
        sync.Action("advance", "main-v2", MERGE_SHA, "measured"),
        sync.Action("delete", name, MERGE_SHA, "superseded"),
    ]
    code, _, landed = sync._apply_actions(repo, actions, "main-v2", "main", TRUNK_TIP, UPSTREAM_TIP, False)
    assert code == sync.EXIT_PRECONDITION
    assert landed is None
    assert fake.refs["main-v2"] == UPSTREAM_TIP
    assert fake.refs[name] == MERGE_SHA
    assert not [w for w in fake.writes if w[0] == "DELETE"]


def test_a_sync_merge_whose_second_parent_is_not_the_upstream_is_never_deleted() -> None:
    """Sol P0 on #5728: a commit whose second parent only shares the upstream's twelve characters is not the
    merge the name records. The delete must prove the second parent is reachable from the upstream tip."""
    forged_parent = UPSTREAM_TIP[:12] + "0" * 28
    name = f"sync--main-v2--{UPSTREAM_TIP[:12]}"
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP, name: MERGE_SHA})
    fake.commits[MERGE_SHA] = [TRUNK_TIP, forged_parent]
    fake.commits[forged_parent] = []
    repo = sync.Repo("o/r", gh=fake)
    with pytest.raises(sync.PreconditionError):
        repo.delete_ref_at(name, MERGE_SHA, "main-v2", "main")
    assert fake.refs[name] == MERGE_SHA
    # Positive control: the same shape with the parent really in the upstream's history is deletable.
    fake.commits[UPSTREAM_TIP] = [forged_parent]
    repo.delete_ref_at(name, MERGE_SHA, "main-v2", "main")
    assert name not in fake.refs


def test_a_conflict_list_too_long_for_a_pr_body_is_refused_not_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sol P1 on #5728: the PR body must name every conflicted path. A list that cannot fit is refused,
    not cut at twenty paths with nothing saying so."""
    monkeypatch.setattr(sync, "conflicted_paths", lambda *_: [f"pkg/module_{i:04d}/" + "x" * 200 for i in range(200)])
    monkeypatch.setattr(sync, "PATH_LIST_LIMIT", 1000)
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP}, merge_status=409)
    with pytest.raises(sync.PreconditionError):
        _run(fake)
    assert not [w for w in fake.writes if w[1] == "pulls"]


def test_a_plain_sync_among_more_than_a_hundred_conflict_refs_is_still_found() -> None:
    """Codex P2 on #5728: closed conflict refs are kept, so the matching-refs answer can be long. The plain sync
    must be found in that one answer, or the trunk would try to create it again and get a 422."""
    name = f"sync--main-v2--{MERGE_SHA[:12]}"
    refs = {f"sync--main-v2--conflict--{i:012x}": TRUNK_TIP for i in range(120)}
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP, **refs, name: MERGE_SHA})
    fake.commits[MERGE_SHA] = [TRUNK_TIP, UPSTREAM_TIP]
    repo = sync.Repo("o/r", gh=fake)
    assert [r.name for r in repo.sync_refs("main-v2", TRUNK_TIP, REQUIRED)] == [name]


def test_the_conflict_pr_merged_last_decides_not_the_one_created_last() -> None:
    """Sol P1 on #5728: creation order is not merge order. The squash (#41, created first)
    merged last, so it must block even though #42 was created after it."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.commits[TRUNK_TIP] = ["e" * 40]
    created_last_merged_first = {
        **_merged_conflict_pr("e" * 40),
        "number": 42,
        "merged_at": "2026-10-10T10:00:00Z",
    }
    created_first_merged_last = _merged_conflict_pr("f" * 40)
    fake.closed_pulls = [created_last_merged_first, created_first_merged_last]
    code, lines = _run(fake)
    assert code == sync.EXIT_NEEDS_HUMAN
    assert fake.writes == []
    assert "#41" in "".join(lines)


def test_an_unknown_only_trunk_fails_loudly_and_writes_nothing(tmp_path: Path) -> None:
    """Sol P1 on #5728: a mistyped --only skipped every trunk and still returned green."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    code = sync.main(
        ["--config", str(_two_trunk_config(tmp_path)), "--only", "main-v20"],
        repo=sync.Repo("o/r", gh=fake),
    )
    assert code == sync.EXIT_PRECONDITION
    assert fake.writes == []


def _two_trunk_config(tmp_path: Path) -> Path:
    config = tmp_path / "trunks.json"
    trunks = [{"branch": "main-v9", "upstream": "main"}, {"branch": "main-v2", "upstream": "main"}]
    config.write_text(json.dumps({"required_checks": REQUIRED, "trunks": trunks}))
    return config


def test_one_unmeasurable_trunk_does_not_stop_the_others(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTEGRATION_SYNC_WRITES", "true")
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    code = sync.main(["--config", str(_two_trunk_config(tmp_path))], repo=sync.Repo("o/r", gh=fake))
    assert code == sync.EXIT_PRECONDITION
    assert fake.refs["sync--main-v2--" + UPSTREAM_TIP[:12]] == MERGE_SHA


def test_without_the_writes_switch_nothing_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTEGRATION_SYNC_WRITES", raising=False)
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    code = sync.main(["--config", str(_two_trunk_config(tmp_path))], repo=sync.Repo("o/r", gh=fake))
    assert code == sync.EXIT_PRECONDITION, "main-v9 is still missing"
    assert fake.writes == []


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


def test_a_ref_payload_without_a_sha_is_unknown_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, fake = _main_with_payloads(tmp_path, monkeypatch, capsys, {"git/ref/heads/main-v2": {"ref": "x"}})
    assert code == sync.EXIT_PRECONDITION
    assert "| main-v2 | UNKNOWN | unexpected GitHub payload: KeyError" in out
    assert fake.writes == []


def test_a_compare_payload_without_a_count_is_unknown_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, fake = _main_with_payloads(tmp_path, monkeypatch, capsys, {"compare/": {}})
    assert code == sync.EXIT_PRECONDITION
    assert "| main-v2 | UNKNOWN | unexpected GitHub payload: KeyError" in out
    assert fake.writes == []


def test_a_check_run_payload_without_its_fields_is_unknown_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sync_ref = {"ref": "refs/heads/sync--main-v2--" + UPSTREAM_TIP[:12], "object": {"sha": MERGE_SHA}}
    code, out, fake = _main_with_payloads(
        tmp_path,
        monkeypatch,
        capsys,
        {"git/matching-refs": [sync_ref], "check-runs": {"total_count": 0}},
    )
    assert code == sync.EXIT_PRECONDITION
    assert "| main-v2 | UNKNOWN | unexpected GitHub payload: KeyError" in out
    assert fake.writes == []


def test_a_commit_payload_without_parents_is_unknown_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sync_ref = {"ref": "refs/heads/sync--main-v2--" + "a" * 12, "object": {"sha": "c" * 40}}
    commit = {"commit": {"committer": {"date": "2026-10-17T00:00:00Z"}}}
    code, out, fake = _main_with_payloads(
        tmp_path, monkeypatch, capsys, {"git/matching-refs": [sync_ref], "commits/": commit}
    )
    assert code == sync.EXIT_PRECONDITION
    assert "| main-v2 | UNKNOWN | unexpected GitHub payload: KeyError" in out
    assert fake.writes == []


def test_a_non_json_gh_answer_is_unknown_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    class Done:
        stdout = "HTTP/2 502 Bad Gateway\r\n\r\n<html>bad gateway</html>"
        stderr = ""

    monkeypatch.setattr(sync, "_resolve", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(sync.subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(sync.PreconditionError, match="not JSON"):
        sync.gh_call("GET", "repos/x/y/git/ref/heads/main")


def test_a_payload_shape_the_reads_miss_still_leaves_the_other_trunks_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("INTEGRATION_SYNC_WRITES", raising=False)
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    real_run_trunk = sync.run_trunk

    def malformed_first(repo, trunk, *args, **kwargs):  # type: ignore[no-untyped-def]
        if trunk == "main-v9":
            raise KeyError("pulls[0]['number']")
        return real_run_trunk(repo, trunk, *args, **kwargs)

    monkeypatch.setattr(sync, "run_trunk", malformed_first)
    code = sync.main(["--config", str(_two_trunk_config(tmp_path))], repo=sync.Repo("o/r", gh=fake))
    assert code == sync.EXIT_PRECONDITION
    assert fake.writes == []
    # Sol P1 on #5728: prove the second trunk ran, not only that the aggregate exit code is right.
    assert "| main-v2 |" in capsys.readouterr().out, "main() stopped after the malformed trunk"


def test_an_advance_for_an_older_upstream_starts_the_latest_sync_in_the_same_pass() -> None:
    """Codex P2 on #5728: advancing an older green sync must not end the daily pass before
    the trunk's current upstream tip gets its own sync."""
    older_merge = "d" * 40
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.commits[older_merge] = [TRUNK_TIP, "e" * 40]
    fake.refs["sync--main-v2--" + "e" * 12] = older_merge
    _green(fake, older_merge)
    code, _ = _run(fake, advance_only=False)
    assert code == sync.EXIT_OK
    assert fake.refs["main-v2"] == older_merge, "the older green sync advanced the trunk"
    assert "sync--main-v2--" + UPSTREAM_TIP[:12] in fake.refs, "the latest upstream tip got no sync"


def test_an_advance_in_the_hourly_pass_also_starts_the_latest_sync() -> None:
    """Sol P1 on #5728: the hourly advance-only pass must also start the latest upstream tip's sync
    after it advances an older green sync, or the trunk waits for the next daily pass."""
    older_merge = "d" * 40
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.commits[older_merge] = [TRUNK_TIP, "e" * 40]
    fake.refs["sync--main-v2--" + "e" * 12] = older_merge
    _green(fake, older_merge)
    code, _ = _run(fake, advance_only=True)
    assert code == sync.EXIT_OK
    assert fake.refs["main-v2"] == older_merge
    assert "sync--main-v2--" + UPSTREAM_TIP[:12] in fake.refs, "the hourly pass left the latest tip unsynced"


def test_an_advance_starts_the_sync_of_a_downstream_trunk_in_the_same_hourly_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2 on #5728: main-v3 is fed by main-v2. When the hourly pass advances main-v2 earlier
    in the loop, main-v3 must start its sync in that pass, not report idle until the daily pass."""
    monkeypatch.setenv("INTEGRATION_SYNC_WRITES", "true")
    monkeypatch.setenv("INTEGRATION_SYNC_AUTO_FF", "true")
    older_merge = "d" * 40
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP, "main-v3": "9" * 40})
    fake.commits[older_merge] = [TRUNK_TIP, "e" * 40]
    fake.refs["sync--main-v2--" + "e" * 12] = older_merge
    _green(fake, older_merge)
    config = tmp_path / "trunks.json"
    trunks = [{"branch": "main-v2", "upstream": "main"}, {"branch": "main-v3", "upstream": "main-v2"}]
    config.write_text(json.dumps({"required_checks": REQUIRED, "trunks": trunks}))
    code = sync.main(["--config", str(config), "--advance-only"], repo=sync.Repo("o/r", gh=fake))
    assert code == sync.EXIT_OK
    assert fake.refs["main-v2"] == older_merge
    assert any(name.startswith("sync--main-v3--") for name in fake.refs), "main-v3 waited for the daily pass"


def test_an_advance_rebuilds_a_latest_sync_built_on_the_old_trunk() -> None:
    """Sol P1 on #5728: a live sync for the latest tip whose first parent is the pre-advance trunk
    must be rebuilt on the advanced trunk in the same pass. Both syncs are seeded before the run
    under test, so the run has to do the rebuild itself."""
    older_merge = "d" * 40
    latest = "sync--main-v2--" + UPSTREAM_TIP[:12]
    pre_advance_latest = "7" * 40  # distinct from MERGE_SHA, which the fake returns for the rebuild
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.commits[older_merge] = [TRUNK_TIP, "e" * 40]
    fake.refs["sync--main-v2--" + "e" * 12] = older_merge
    _green(fake, older_merge)
    fake.commits[pre_advance_latest] = [TRUNK_TIP, UPSTREAM_TIP]
    fake.refs[latest] = pre_advance_latest
    code, _ = _run(fake, advance_only=True)
    assert code == sync.EXIT_OK
    assert fake.refs["main-v2"] == older_merge
    assert fake.refs[latest] != pre_advance_latest, "the stale latest sync was not rebuilt"
    assert fake.commits[fake.refs[latest]][0] == older_merge, "the latest sync kept the pre-advance first parent"


def test_a_204_merge_against_a_behind_trunk_is_unknown_not_green() -> None:
    """Sol P1 on #5728: "nothing to merge" contradicts a compare that says the trunk is behind.
    The job must not delete its ref and report green without CI."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP}, merge_status=204)
    with pytest.raises(sync.PreconditionError, match="merge answered 204"):
        _run(fake)
    assert "sync--main-v2--" + UPSTREAM_TIP[:12] not in fake.refs
    assert fake.refs["main-v2"] == TRUNK_TIP


def test_the_credential_helper_quotes_a_gh_path_with_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex P2 on #5728: git runs a `!` credential helper as a shell snippet, so an unquoted gh path
    with spaces (C:\\Program Files\\GitHub CLI\\gh.exe) splits at the first space."""
    seen: list[list[str]] = []

    class Done:
        returncode = 1
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(list(argv))
        return Done()

    gh_path = "C:/Program Files/GitHub CLI/gh.exe"
    monkeypatch.setattr(sync, "_resolve", lambda name: "/usr/bin/git" if name == "git" else gh_path)
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    assert sync.conflicted_paths("a" * 40, "b" * 40) is None
    helper = next(arg for arg in seen[0] if arg.startswith("credential.helper=!"))
    assert helper == f'credential.helper=!"{gh_path}" auth git-credential'


def test_a_newer_squash_is_not_hidden_by_a_later_merge_of_an_older_branch() -> None:
    """Codex P1 on #5728: the newest merge (an old branch, reopened and merged last) contains its own
    head, but the squash merged before it is still missing from the trunk, so the trunk must block."""
    fake = FakeGitHub({"main": UPSTREAM_TIP, "main-v2": TRUNK_TIP})
    fake.commits[TRUNK_TIP] = ["d" * 40]
    squashed_newer = {**_merged_conflict_pr("e" * 40), "number": 42, "merged_at": "2026-10-12T10:00:00Z"}
    older_merged_last = {**_merged_conflict_pr("d" * 40), "number": 43, "merged_at": "2026-10-14T10:00:00Z"}
    fake.closed_pulls = [squashed_newer, older_merged_last]
    code, lines = _run(fake)
    assert code == sync.EXIT_NEEDS_HUMAN
    assert fake.writes == []
    assert "#42" in "".join(lines)


def test_a_local_run_without_repo_asks_gh_and_refuses_when_gh_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Outside Actions GITHUB_REPOSITORY is unset, so the repository must come from gh or the run stops.
    monkeypatch.setattr(sync, "_resolve", lambda name: None)
    with pytest.raises(sync.PreconditionError, match="no gh on PATH"):
        sync._repo_from("")
    gh_answer = SimpleNamespace(returncode=0, stdout="private_owner/music-dj-tools\n", stderr="")
    monkeypatch.setattr(sync, "_resolve", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(sync.subprocess, "run", lambda *a, **k: gh_answer)
    assert sync._repo_from("").slug == "private_owner/music-dj-tools"
