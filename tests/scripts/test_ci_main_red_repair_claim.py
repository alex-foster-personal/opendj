"""``scripts.ci_main_red_repair_claim`` serializes main-red repair work (X26).

Regression lines:
  - if two owners can claim the same main-red set concurrently then broken
  - if main is green and try succeeds then broken
  - if release clears only the matching owner then broken
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scripts.ci_main_red import MainRed
from scripts.ci_main_red_repair_claim import (
    EXIT_ERROR,
    ClaimRecord,
    GithubCoordinationError,
    GITHUB_RELEASE_MARKER,
    _claim_labels,
    _coord_mode,
    _flatten_github_pages,
    _load_file_claims,
    claim_file_paths,
    claim_key,
    identities_key,
    main,
    parse_github_active_claims,
    parse_github_claim_thread,
    release,
    reset_cli_claim_dir,
    reset_gh_executable_cache,
    set_gh_runner,
    try_claim,
)
from tests.scripts.gh_main_red_repair_claim_replay import install_main_red_repair_gh_replay


def _red(main_sha: str, identities: frozenset[str]) -> MainRed:
    return MainRed(
        identities=identities,
        failed_job_names=frozenset(),
        main_sha=main_sha,
        measured_sha=main_sha,
    )


@pytest.fixture()
def claim_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ci-watch"
    monkeypatch.setenv("OPENDJ_CI_WATCH_DIR", str(root))
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_COORD", "file")
    reset_cli_claim_dir()
    reset_gh_executable_cache()
    set_gh_runner(None)
    return root


def test_try_claim_blocks_a_second_owner(claim_dir: Path) -> None:
    red = _red("a" * 40, frozenset({"FAILED tests/x.py::test_one"}))
    ok1, _ = try_claim("owner-a", red=red, now=1000.0, claim_dir=claim_dir)
    ok2, msg2 = try_claim("owner-b", red=red, now=1001.0, claim_dir=claim_dir)
    assert ok1 is True
    assert ok2 is False
    assert "owner-a" in msg2


def test_green_main_refuses_claim(claim_dir: Path) -> None:
    red = _red("b" * 40, frozenset())
    ok, msg = try_claim("owner-a", red=red, claim_dir=claim_dir)
    assert ok is False
    assert "not red" in msg


def test_release_only_for_the_holder(claim_dir: Path) -> None:
    red = _red("c" * 40, frozenset({"FAILED tests/y.py::test_two"}))
    try_claim("owner-a", red=red, now=2000.0, claim_dir=claim_dir)
    ok_wrong, _ = release("owner-b", claim_dir=claim_dir)
    assert ok_wrong is False
    ok_right, _ = release("owner-a", claim_dir=claim_dir)
    assert ok_right is True
    claim_file, _ = claim_file_paths(claim_dir)
    assert not claim_file.exists()


def test_identities_key_is_stable() -> None:
    ids = frozenset({"FAILED b", "FAILED a"})
    assert identities_key(ids) == identities_key(frozenset({"FAILED a", "FAILED b"}))


def test_unreadable_job_only_main_is_red_and_claimable(claim_dir: Path) -> None:
    red = MainRed(
        identities=frozenset(),
        failed_job_names=frozenset(),
        main_sha="d" * 40,
        unreadable_job_names=frozenset({"Bundle budgets (per-surface gzip ceilings)"}),
        measured_sha="d" * 40,
    )
    ok, msg = try_claim("owner-a", red=red, now=3000.0, claim_dir=claim_dir)
    assert ok is True
    assert "claimed" in msg
    ok2, _ = try_claim("owner-b", red=red, now=3001.0, claim_dir=claim_dir)
    assert ok2 is False


def test_second_owner_blocked_when_unreadable_job_becomes_readable(claim_dir: Path) -> None:
    job = "Bundle budgets (per-surface gzip ceilings)"
    red_unreadable = MainRed(
        identities=frozenset(),
        failed_job_names=frozenset(),
        unreadable_job_names=frozenset({job}),
        main_sha="7" * 40,
        measured_sha="7" * 40,
    )
    red_readable = MainRed(
        identities=frozenset({"FAILED tests/foo.py::bar"}),
        failed_job_names=frozenset({job}),
        unreadable_job_names=frozenset(),
        main_sha="7" * 40,
        measured_sha="7" * 40,
    )
    try_claim("owner-a", red=red_unreadable, now=10000.0, claim_dir=claim_dir)
    ok, msg = try_claim("owner-b", red=red_readable, now=10001.0, claim_dir=claim_dir)
    assert ok is False
    assert "owner-a" in msg


def test_failed_job_names_without_identities_share_claim_key(claim_dir: Path) -> None:
    red = MainRed(
        identities=frozenset(),
        failed_job_names=frozenset({"quality-ratchet"}),
        main_sha="e" * 40,
        measured_sha="e" * 40,
    )
    assert claim_key(red) == claim_key(red)
    ok, _ = try_claim("owner-a", red=red, now=4000.0, claim_dir=claim_dir)
    assert ok is True


def test_main_returns_exit_error_not_exit_held_on_corrupt_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corrupt = tmp_path / "main-red.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr("scripts.ci_main_red_repair_claim.MAIN_RED_CACHE", corrupt)
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_COORD", "file")
    reset_cli_claim_dir()
    assert main(["try", "--owner", "agent"]) == EXIT_ERROR


def test_claim_key_includes_identities_and_unreadable_jobs() -> None:
    red = MainRed(
        identities=frozenset({"FAILED tests/a.py::t"}),
        failed_job_names=frozenset(),
        unreadable_job_names=frozenset({"Bundle budgets (per-surface gzip ceilings)"}),
        main_sha="x" * 40,
        measured_sha="x" * 40,
    )
    key = claim_key(red)
    red_jobs_only = MainRed(
        identities=frozenset(),
        failed_job_names=frozenset(),
        unreadable_job_names=frozenset({"Bundle budgets (per-surface gzip ceilings)"}),
        main_sha="x" * 40,
        measured_sha="x" * 40,
    )
    assert key != claim_key(red_jobs_only)


def test_flatten_github_pages_handles_slurp_shape() -> None:
    page_one = [{"id": 1, "body": "a"}]
    page_two = [{"id": 2, "body": "b"}]
    assert _flatten_github_pages([page_one, page_two]) == page_one + page_two
    assert _flatten_github_pages(page_one) == page_one


def test_parse_github_claim_thread_earliest_comment_id_wins() -> None:
    payload_early = json.dumps(
        {
            "main_sha": "a" * 40,
            "identities_key": "early",
            "owner": "owner-early",
            "claimed_at_unix": 1.0,
            "identities": ["id:FAILED x"],
        }
    )
    payload_late = json.dumps(
        {
            "main_sha": "a" * 40,
            "identities_key": "late",
            "owner": "owner-late",
            "claimed_at_unix": 2.0,
            "identities": ["id:FAILED x"],
        }
    )
    comments = [
        {
            "id": 100,
            "created_at": "2026-01-01T00:00:00Z",
            "body": f"<!-- main-red-repair-claim v1 -->\n{payload_early}",
        },
        {
            "id": 200,
            "created_at": "2026-01-01T00:01:00Z",
            "body": f"<!-- main-red-repair-claim v1 -->\n{payload_late}",
        },
    ]
    winner = parse_github_claim_thread(comments)
    assert winner is not None and winner.owner == "owner-early"


def test_coord_mode_auto_uses_github_when_gh_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_COORD", "auto")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    assert _coord_mode() == "both"


def test_unknown_coord_mode_exits_error(
    claim_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_COORD", "not-a-mode")
    reset_cli_claim_dir()
    code = main(["status"])
    assert code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "not-a-mode" in err


def test_same_owner_refreshes_claim_when_main_advances_but_identity_overlaps(
    claim_dir: Path,
) -> None:
    identity = "FAILED tests/z.py::test_three"
    red_v1 = _red("f" * 40, frozenset({identity}))
    red_v2 = _red("0" * 40, frozenset({identity}))
    try_claim("owner-a", red=red_v1, now=5000.0, claim_dir=claim_dir)
    ok, msg = try_claim("owner-a", red=red_v2, now=5001.0, claim_dir=claim_dir)
    assert ok is True
    assert "refreshed" in msg
    saved = _load_file_claims(claim_dir)
    assert len(saved) == 1 and saved[0].main_sha == "0" * 40


def test_second_owner_blocked_when_main_advances_but_identity_overlaps(claim_dir: Path) -> None:
    identity = "FAILED tests/z.py::test_four"
    red_v1 = _red("1" * 40, frozenset({identity}))
    red_v2 = _red("2" * 40, frozenset({identity}))
    try_claim("owner-a", red=red_v1, now=6000.0, claim_dir=claim_dir)
    ok, msg = try_claim("owner-b", red=red_v2, now=6001.0, claim_dir=claim_dir)
    assert ok is False
    assert "owner-a" in msg


def test_parse_github_claim_thread_release_clears() -> None:
    payload = json.dumps(
        {
            "main_sha": "a" * 40,
            "identities_key": "abc",
            "owner": "agent-1",
            "claimed_at_unix": 1.0,
            "identities": ["FAILED x"],
        }
    )
    comments: list[dict[str, object]] = [
        {"created_at": "2026-01-01T00:00:00Z", "body": f"{payload}\n{''}"},
    ]
    comments[0]["body"] = f"<!-- main-red-repair-claim v1 -->\n{payload}"
    active = parse_github_claim_thread(comments)
    assert active is not None and active.owner == "agent-1"
    comments.append(
        {
            "created_at": "2026-01-01T01:00:00Z",
            "body": "<!-- main-red-repair-release v1 --> owner=agent-1",
        }
    )
    assert parse_github_claim_thread(comments) is None


def test_github_remote_blocks_overlapping_owner(claim_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_COORD", "both")
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_ISSUE", "4046")
    store: dict[str, list[dict[str, object]]] = {"comments": []}

    def fake_gh(args: list[str]) -> str:
        if args[:2] == ["api", "-X"]:
            store["comments"].append(
                {
                    "id": len(store["comments"]) + 1,
                    "created_at": "t",
                    "body": args[-1].split("=", 1)[-1],
                }
            )
            return "{}"
        if "issues/4046/comments" in args[1]:
            return json.dumps([store["comments"]])
        raise AssertionError(args)

    set_gh_runner(fake_gh)
    red = _red("3" * 40, frozenset({"FAILED tests/gh.py::test_one"}))
    record = ClaimRecord(
        main_sha=red.main_sha,
        identities_key=claim_key(red),
        owner="remote-owner",
        claimed_at_unix=7000.0,
        identities=_claim_labels(red),
    )
    store["comments"].append(
        {
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "body": "<!-- main-red-repair-claim v1 -->\n"
            + json.dumps(
                {
                    "main_sha": record.main_sha,
                    "identities_key": record.identities_key,
                    "owner": record.owner,
                    "claimed_at_unix": record.claimed_at_unix,
                    "identities": list(record.identities),
                }
            ),
        }
    )
    ok, msg = try_claim("local-owner", red=red, now=7001.0, claim_dir=claim_dir)
    assert ok is False
    assert "remote-owner" in msg


def test_github_race_loser_backs_off_after_post(
    claim_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_COORD", "both")
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_ISSUE", "4046")
    red = _red("3" * 40, frozenset({"FAILED tests/race.py::one"}))
    key = claim_key(red)
    winner_body = "<!-- main-red-repair-claim v1 -->\n" + json.dumps(
        {
            "main_sha": red.main_sha,
            "identities_key": key,
            "owner": "winner",
            "claimed_at_unix": 8000.0,
            "identities": list(_claim_labels(red)),
        }
    )
    inject_on_post = [
        {
            "id": 10,
            "created_at": "2026-01-01T00:00:00Z",
            "body": winner_body,
        }
    ]
    reset_gh_executable_cache()
    set_gh_runner(None)
    replay = install_main_red_repair_gh_replay(monkeypatch, tmp_path, seed_comments=[], inject_on_post=inject_on_post)
    ok, msg = try_claim("loser", red=red, now=8001.0, claim_dir=claim_dir)
    assert ok is False
    assert "winner" in msg
    claim_file, _ = claim_file_paths(claim_dir)
    assert not claim_file.exists()
    assert any(
        GITHUB_RELEASE_MARKER in str(c.get("body")) and "owner=loser" in str(c.get("body")) for c in replay.comments
    )
    assert any(len(inv) >= 3 and inv[1:3] == ["api", "-X"] for inv in replay.invocations)


def test_release_clears_remote_when_local_belongs_to_other_owner(
    claim_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two hosts: local claim file from host A, remote GitHub claim for owner B on host B."""
    red_local = _red("8" * 40, frozenset({"FAILED tests/local.py::one"}))
    red_remote = _red("8" * 40, frozenset({"FAILED tests/remote.py::two"}))
    try_claim("owner-a", red=red_local, now=10000.0, claim_dir=claim_dir)
    claim_file, _ = claim_file_paths(claim_dir)
    assert {c.owner for c in _load_file_claims(claim_dir)} == {"owner-a"}

    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_COORD", "both")
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_ISSUE", "4046")

    store: dict[str, list[dict[str, object]]] = {"comments": []}
    remote_claimed_at = time.time()
    store["comments"].append(
        {
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "body": "<!-- main-red-repair-claim v1 -->\n"
            + json.dumps(
                {
                    "main_sha": red_remote.main_sha,
                    "identities_key": claim_key(red_remote),
                    "owner": "owner-b",
                    "claimed_at_unix": remote_claimed_at,
                    "identities": list(_claim_labels(red_remote)),
                }
            ),
        }
    )

    def fake_gh(args: list[str]) -> str:
        if args[:2] == ["api", "-X"]:
            store["comments"].append(
                {
                    "id": len(store["comments"]) + 1,
                    "created_at": "2026-01-01T01:00:00Z",
                    "body": args[-1].split("=", 1)[-1],
                }
            )
            return "{}"
        if "issues/4046/comments" in args[1]:
            return json.dumps([store["comments"]])
        raise AssertionError(args)

    set_gh_runner(fake_gh)
    ok, msg = release("owner-b", claim_dir=claim_dir)
    assert ok is True
    assert "released" in msg
    assert "owner-a" in msg
    assert "unchanged" in msg
    assert {c.owner for c in _load_file_claims(claim_dir)} == {"owner-a"}
    assert any(
        GITHUB_RELEASE_MARKER in str(c.get("body")) and "owner=owner-b" in str(c.get("body")) for c in store["comments"]
    )


def test_release_clears_remote_without_local_file(claim_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_COORD", "both")
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_ISSUE", "4046")
    store: dict[str, list[dict[str, object]]] = {"comments": []}
    red = _red("4" * 40, frozenset({"FAILED tests/rel.py::one"}))
    claimed_at = time.time()
    record = ClaimRecord(
        main_sha=red.main_sha,
        identities_key=claim_key(red),
        owner="remote-only",
        claimed_at_unix=claimed_at,
        identities=_claim_labels(red),
    )
    store["comments"].append(
        {
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "body": "<!-- main-red-repair-claim v1 -->\n"
            + json.dumps(
                {
                    "main_sha": record.main_sha,
                    "identities_key": record.identities_key,
                    "owner": record.owner,
                    "claimed_at_unix": record.claimed_at_unix,
                    "identities": list(record.identities),
                }
            ),
        }
    )

    def fake_gh(args: list[str]) -> str:
        if args[:2] == ["api", "-X"]:
            store["comments"].append(
                {
                    "id": len(store["comments"]) + 1,
                    "created_at": "2026-01-01T01:00:00Z",
                    "body": args[-1].split("=", 1)[-1],
                }
            )
            return "{}"
        if "issues/4046/comments" in args[1]:
            return json.dumps([store["comments"]])
        raise AssertionError(args)

    set_gh_runner(fake_gh)
    ok, msg = release("remote-only", claim_dir=claim_dir)
    assert ok is True
    assert "released" in msg
    assert any(GITHUB_RELEASE_MARKER in str(c.get("body")) for c in store["comments"])


def test_parse_github_active_claims_keeps_disjoint_owners() -> None:
    comments = [
        {
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "body": "<!-- main-red-repair-claim v1 -->\n"
            + json.dumps(
                {
                    "main_sha": "a" * 40,
                    "identities_key": "key-a",
                    "owner": "owner-a",
                    "claimed_at_unix": 1.0,
                    "identities": ["id:FAILED a"],
                }
            ),
        },
        {
            "id": 2,
            "created_at": "2026-01-01T00:01:00Z",
            "body": "<!-- main-red-repair-claim v1 -->\n"
            + json.dumps(
                {
                    "main_sha": "a" * 40,
                    "identities_key": "key-b",
                    "owner": "owner-b",
                    "claimed_at_unix": 2.0,
                    "identities": ["id:FAILED b"],
                }
            ),
        },
    ]
    active = parse_github_active_claims(comments)
    owners = {record.owner for _cid, record in active}
    assert owners == {"owner-a", "owner-b"}


def test_file_mode_disjoint_claims_preserve_other_owners(claim_dir: Path) -> None:
    """[if] A holds X and B claims disjoint Y [then] A's record stays [else stop]."""
    main_sha = "9" * 40
    red_x = _red(main_sha, frozenset({"FAILED tests/x.py::one"}))
    red_y = _red(main_sha, frozenset({"FAILED tests/y.py::two"}))
    ok_a, _ = try_claim("owner-a", red=red_x, now=11000.0, claim_dir=claim_dir)
    ok_b, _ = try_claim("owner-b", red=red_y, now=11001.0, claim_dir=claim_dir)
    assert ok_a is True
    assert ok_b is True
    saved = _load_file_claims(claim_dir)
    owners = {record.owner for record in saved}
    assert owners == {"owner-a", "owner-b"}
    ok_c, msg_c = try_claim("owner-c", red=red_x, now=11002.0, claim_dir=claim_dir)
    assert ok_c is False
    assert "owner-a" in msg_c


def test_same_owner_disjoint_claims_preserve_both_failures(claim_dir: Path) -> None:
    """[if] owner holds X then claims disjoint Y [then] X stays live [else stop]."""
    main_sha = "8" * 40
    red_x = _red(main_sha, frozenset({"FAILED tests/x.py::one"}))
    red_y = _red(main_sha, frozenset({"FAILED tests/y.py::two"}))
    ok_x, _ = try_claim("owner-a", red=red_x, now=12000.0, claim_dir=claim_dir)
    ok_y, _ = try_claim("owner-a", red=red_y, now=12001.0, claim_dir=claim_dir)
    assert ok_x is True
    assert ok_y is True
    saved = _load_file_claims(claim_dir)
    keys = {record.identities_key for record in saved}
    assert keys == {claim_key(red_x), claim_key(red_y)}
    ok_c, msg_c = try_claim("owner-b", red=red_x, now=12002.0, claim_dir=claim_dir)
    assert ok_c is False
    assert "owner-a" in msg_c


def test_parse_github_active_claims_keeps_disjoint_same_owner() -> None:
    comments = [
        {
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "body": "<!-- main-red-repair-claim v1 -->\n"
            + json.dumps(
                {
                    "main_sha": "a" * 40,
                    "identities_key": "key-a",
                    "owner": "owner-a",
                    "claimed_at_unix": 1.0,
                    "identities": ["id:FAILED a"],
                }
            ),
        },
        {
            "id": 2,
            "created_at": "2026-01-01T00:01:00Z",
            "body": "<!-- main-red-repair-claim v1 -->\n"
            + json.dumps(
                {
                    "main_sha": "a" * 40,
                    "identities_key": "key-b",
                    "owner": "owner-a",
                    "claimed_at_unix": 2.0,
                    "identities": ["id:FAILED b"],
                }
            ),
        },
    ]
    active = parse_github_active_claims(comments)
    keys = {record.identities_key for _cid, record in active}
    assert keys == {"key-a", "key-b"}


def test_third_owner_blocked_by_disjoint_remote_claim(claim_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_COORD", "both")
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_ISSUE", "4046")
    red_a = _red("5" * 40, frozenset({"FAILED tests/a.py::one"}))
    red_b = _red("5" * 40, frozenset({"FAILED tests/b.py::two"}))
    store: dict[str, list[dict[str, object]]] = {"comments": []}
    for owner, red in (("owner-a", red_a), ("owner-b", red_b)):
        store["comments"].append(
            {
                "id": len(store["comments"]) + 1,
                "created_at": f"2026-01-01T00:0{len(store['comments'])}:00Z",
                "body": "<!-- main-red-repair-claim v1 -->\n"
                + json.dumps(
                    {
                        "main_sha": red.main_sha,
                        "identities_key": claim_key(red),
                        "owner": owner,
                        "claimed_at_unix": 9000.0,
                        "identities": list(_claim_labels(red)),
                    }
                ),
            }
        )

    def fake_gh(args: list[str]) -> str:
        if "issues/4046/comments" in args[1]:
            return json.dumps([store["comments"]])
        raise AssertionError(args)

    set_gh_runner(fake_gh)
    ok, msg = try_claim("owner-c", red=red_b, now=9001.0, claim_dir=claim_dir)
    assert ok is False
    assert "owner-b" in msg


def test_github_mode_fails_closed_when_issue_unavailable(claim_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENDJ_MAIN_RED_REPAIR_COORD", "github")
    monkeypatch.delenv("OPENDJ_MAIN_RED_REPAIR_ISSUE", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def failing_gh(_args: list[str]) -> str:
        raise OSError("network down")

    set_gh_runner(failing_gh)
    red = _red("6" * 40, frozenset({"FAILED tests/fail.py::closed"}))
    with pytest.raises(GithubCoordinationError):
        try_claim("agent", red=red, now=100.0, claim_dir=claim_dir)


def test_module_imports_without_fcntl_directly() -> None:
    source = Path("scripts/ci_main_red_repair_claim.py").read_text(encoding="utf-8")
    assert "import fcntl" not in source
