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

import pytest

from scripts import integration_sync as sync
from tests.scripts.integration_sync_helpers import (
    MERGE_SHA,
    MOVED_TIP,
    REQUIRED,
    TRUNK_TIP,
    UPSTREAM_TIP,
    FakeGitHub,
    _green,
    _is_plain_sync,
    _kinds,
    _plan,
    _ref,
    _run,
)


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
