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
from tests.scripts.integration_sync_helpers import (
    MERGE_SHA,
    MOVED_TIP,
    REQUIRED,
    TRUNK_TIP,
    UPSTREAM_TIP,
    FakeGitHub,
    _closed_feature_prs,
    _green,
    _is_plain_sync,
    _main_with_payloads,
    _merged_conflict_pr,
    _run,
    _two_trunk_config,
)


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
