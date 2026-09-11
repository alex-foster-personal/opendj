"""``python -m apps.sync_hub policy``: exit codes, dry-run, and parity with HTTP.

The parity tests feed the SAME change set to the CLI and to the HTTP twin and
require the same JSON back, on the same DB (read verbs, dry runs) or on
byte-identical copies of it (live applies).

- [if] the CLI and HTTP answer one change set differently [then] fail, [else stop].
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from apps.cloud.policy import CFG as FILE_POLICY
from apps.shared.state import db as state_db
from apps.shared.state.migrations_v10 import ASSET_KIND_CHECK_VALUES
from apps.sync_hub import client as sync_client
from apps.sync_hub import maintenance, maintenance_policy
from apps.sync_hub.data_classes import registry_payload
from apps.sync_hub.policy_store import POLICIES_TABLE

from .policy_fixtures import HUB_ID, PLAYLIST_ID, count, http_client, local_id, make_fleet_dir

pytestmark = pytest.mark.requirement("CLOUDSYNC-01")

POLICY_ROWS = "SELECT COUNT(*) FROM sync_policies"
LIVE_POLICY_ROWS = "SELECT COUNT(*) FROM sync_policies WHERE deleted_at IS NULL"
CHANGELOG_ROWS = f"SELECT COUNT(*) FROM local_changelog WHERE table_name = '{POLICIES_TABLE}'"


@pytest.fixture
def fleet_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This machine (no cells) plus a hub pinning every kind, and one live playlist."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    return make_fleet_dir(tmp_path, with_hub=True)


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any]:
    code = maintenance.main(["policy", *argv])
    return code, json.loads(capsys.readouterr().out)


def _write(tmp_path: Path, changes: dict[str, Any]) -> str:
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(changes), encoding="utf-8")
    return str(path)


# ----- change sets, each with the verdict it must earn ------------------------


def _all_pinned(me: str) -> dict[str, Any]:
    return {
        "policies": [
            {"machine_id": me, "asset_kind": k, "mode": "pinned"} for k in ASSET_KIND_CHECK_VALUES
        ]
    }


def _budget_on_stream(me: str) -> dict[str, Any]:
    return {
        "policies": [
            {"machine_id": me, "asset_kind": "audio", "mode": "stream", "cache_budget_mb": 512}
        ]
    }


def _unknown_kind(me: str) -> dict[str, Any]:
    return {"policies": [{"machine_id": me, "asset_kind": "waveform_png", "mode": "pinned"}]}


def _exclude_everywhere(me: str) -> dict[str, Any]:
    return {
        "policies": [
            {"machine_id": me, "asset_kind": "audio", "mode": "excluded"},
            {"machine_id": HUB_ID, "asset_kind": "audio", "mode": "excluded"},
        ]
    }


def _unset_hub_cell(me: str) -> dict[str, Any]:
    return {"removed_policies": [{"machine_id": HUB_ID, "asset_kind": "audio"}]}


CASES: dict[str, tuple[Callable[[str], dict[str, Any]], bool]] = {
    "all_pinned": (_all_pinned, False),
    "budget_on_stream": (_budget_on_stream, True),
    "unknown_kind": (_unknown_kind, True),
    "exclude_everywhere": (_exclude_everywhere, True),
    "unset_hub_cell": (_unset_hub_cell, False),
}


# ----- parity -------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(CASES))
def test_cli_and_http_return_identical_outcomes(
    case: str, fleet_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if validate, plan or a dry-run apply differ between the CLI and HTTP then broken"""
    build, expect_blocking = CASES[case]
    changes = build(local_id(fleet_dir))
    proposal = _write(tmp_path, changes)
    before = count(fleet_dir, POLICY_ROWS)
    cli: dict[str, Any] = {}
    for verb in ("validate", "plan", "apply"):
        _, cli[verb] = _cli(capsys, verb, "--data-dir", str(fleet_dir), "--proposal", proposal)
    with http_client(fleet_dir) as http:
        validate = http.post("/api/v1/cloudsync/policies/validate", json=changes)
        plan = http.post("/api/v1/cloudsync/policies/plan", json=changes)
        apply = http.post("/api/v1/cloudsync/policies/apply", json={"changes": changes})
    assert validate.status_code == plan.status_code == 200
    assert validate.json() == cli["validate"]
    assert plan.json() == cli["plan"]
    applied = apply.json()["detail"]["outcome"] if apply.status_code == 409 else apply.json()
    assert apply.status_code == (409 if expect_blocking else 200), apply.text
    assert applied == cli["apply"]
    # Not vacuous: the case earned the verdict it was built for, and touched rows.
    assert cli["plan"]["blocking"] is expect_blocking
    assert cli["plan"]["plan"], "the control: the change set touched at least one row"
    assert count(fleet_dir, POLICY_ROWS) == before, "dry runs never write"


def test_live_apply_writes_the_same_rows_through_either_twin(
    fleet_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a live apply writes different rows via the CLI than via HTTP then broken"""
    twin = tmp_path / "twin"
    shutil.copytree(fleet_dir, twin)
    changes = _all_pinned(local_id(fleet_dir))
    code, cli_outcome = _cli(
        capsys,
        "apply",
        "--data-dir",
        str(fleet_dir),
        "--proposal",
        _write(tmp_path, changes),
        "--live",
    )
    with http_client(twin) as http:
        response = http.post(
            "/api/v1/cloudsync/policies/apply", json={"changes": changes, "dry_run": False}
        )
    assert code == 0 and response.status_code == 200, response.text
    assert response.json() == cli_outcome
    assert cli_outcome["written"] is True
    rows_sql = (
        "SELECT group_concat(machine_id || '/' || asset_kind || '/' || mode, ',') FROM "
        "(SELECT * FROM sync_policies WHERE deleted_at IS NULL ORDER BY machine_id, asset_kind)"
    )
    assert count(fleet_dir, LIVE_POLICY_ROWS) == count(twin, LIVE_POLICY_ROWS) == 12
    assert _scalar(fleet_dir, rows_sql) == _scalar(twin, rows_sql)


def _scalar(data_dir: Path, sql: str) -> str:
    conn = sqlite3.connect(sync_client.state_db_path(data_dir))
    try:
        return str(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


# ----- refusal writes nothing -------------------------------------------------------


def test_blocking_apply_is_refused_and_writes_nothing(
    fleet_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if an apply with an introduced error writes any row or changelog entry then broken"""
    changes = _budget_on_stream(local_id(fleet_dir))
    rows, logged = count(fleet_dir, POLICY_ROWS), count(fleet_dir, CHANGELOG_ROWS)
    with http_client(fleet_dir) as http:
        refused = http.post(
            "/api/v1/cloudsync/policies/apply", json={"changes": changes, "dry_run": False}
        )
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "POLICY_VIOLATION"
    assert [v["rule_id"] for v in detail["outcome"]["violations"] if v["blocking"]] == [
        "cache_budget"
    ]
    code, outcome = _cli(
        capsys,
        "apply",
        "--data-dir",
        str(fleet_dir),
        "--proposal",
        _write(tmp_path, changes),
        "--live",
    )
    assert code == maintenance_policy.EXIT_VIOLATIONS
    assert outcome["written"] is False
    assert (count(fleet_dir, POLICY_ROWS), count(fleet_dir, CHANGELOG_ROWS)) == (rows, logged)
    assert rows == 6, "the control: the hub's six cells are there to be counted"


# ----- verbs and exit codes ---------------------------------------------------------


def test_seed_then_validate_is_the_enrollment_flow(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if seed --class skips the class defaults, or validate fails after it, then broken"""
    data = str(fleet_dir)
    code, _ = _cli(capsys, "validate", "--data-dir", data)
    assert code == maintenance_policy.EXIT_VIOLATIONS, "this machine starts with no cells"
    code, dry = _cli(capsys, "seed", "--data-dir", data, "--machine", "self", "--class", "portable")
    assert (code, dry["written"]) == (0, False)
    assert count(fleet_dir, LIVE_POLICY_ROWS) == 6, "a dry run writes nothing"
    code, live = _cli(
        capsys, "seed", "--data-dir", data, "--machine", "self", "--class", "portable", "--live"
    )
    assert (code, live["written"]) == (0, True)
    code, _ = _cli(capsys, "validate", "--data-dir", data)
    assert code == 0
    _, shown = _cli(capsys, "show", "--data-dir", data, "--machine", "self")
    expected = {
        kind: (
            artifact.default_mode_by_machine_class["portable"],
            artifact.cache_budget_mb
            if artifact.default_mode_by_machine_class["portable"] == "cached"
            else None,
        )
        for kind, artifact in FILE_POLICY.artifacts.items()
    }
    assert {p["asset_kind"]: (p["mode"], p["cache_budget_mb"]) for p in shown["policies"]} == (
        expected
    )


def test_a_repeated_seed_writes_nothing(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if re-running an identical seed adds changelog entries then broken"""
    argv = (
        "seed",
        "--data-dir",
        str(fleet_dir),
        "--machine",
        "self",
        "--class",
        "archive",
        "--live",
    )
    _cli(capsys, *argv)
    logged = count(fleet_dir, CHANGELOG_ROWS)
    code, again = _cli(capsys, *argv)
    assert code == 0
    assert {entry["action"] for entry in again["plan"]} == {"unchanged"}
    assert again["written"] is False, "written reports rows written, not that apply ran"
    assert count(fleet_dir, CHANGELOG_ROWS) == logged


def test_set_unset_pin_unpin_round_trip(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if set, unset, pin or unpin fail to write (or to tombstone) their one row then broken"""
    data = str(fleet_dir)
    me = ("--data-dir", data, "--machine", "self")
    assert (
        _cli(capsys, "set", *me, "--kind", "audio", "--mode", "cached", "--budget", "64", "--live")[
            0
        ]
        == 0
    )
    assert _cli(capsys, "pin", *me, "--playlist", PLAYLIST_ID, "--mode", "pinned", "--live")[0] == 0
    _, shown = _cli(capsys, "show", *me)
    assert shown["policies"] == [
        {
            "machine_id": local_id(fleet_dir),
            "asset_kind": "audio",
            "mode": "cached",
            "cache_budget_mb": 64,
        }
    ]
    assert [p["playlist_id"] for p in shown["pins"]] == [PLAYLIST_ID]
    code, unset = _cli(capsys, "unset", *me, "--kind", "audio", "--live")
    assert (code, unset["written"]) == (0, True)
    assert _cli(capsys, "unpin", *me, "--playlist", PLAYLIST_ID, "--live")[0] == 0
    _, shown = _cli(capsys, "show", *me)
    assert shown == {"policies": [], "pins": []}
    tombstones = "SELECT COUNT(*) FROM sync_policies WHERE deleted_at IS NOT NULL"
    assert count(fleet_dir, tombstones) == 1, "unset is a synced tombstone, not a DELETE"


def test_plan_exits_3_only_when_blocking(
    fleet_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if plan exits nonzero for a clean change or zero for a blocking one then broken"""
    me = local_id(fleet_dir)
    data = str(fleet_dir)
    ok, _ = _cli(
        capsys, "plan", "--data-dir", data, "--proposal", _write(tmp_path, _all_pinned(me))
    )
    bad, _ = _cli(
        capsys, "plan", "--data-dir", data, "--proposal", _write(tmp_path, _budget_on_stream(me))
    )
    assert (ok, bad) == (0, maintenance_policy.EXIT_VIOLATIONS)


def test_validate_with_no_machines_is_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if validate with no registered machines exits anything but 4 then broken"""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    data_dir = tmp_path / "empty"
    state_db.open_rw(sync_client.state_db_path(data_dir)).close()
    code, outcome = _cli(capsys, "validate", "--data-dir", str(data_dir))
    assert code == maintenance_policy.EXIT_INCONCLUSIVE == maintenance.EXIT_INCONCLUSIVE
    assert outcome["measurable"] is False


def test_apply_with_no_machines_is_inconclusive_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if apply on a fleet with no registered machines exits 0 or reports written then broken"""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    data_dir = tmp_path / "empty"
    state_db.open_rw(sync_client.state_db_path(data_dir)).close()
    proposal = _write(tmp_path, {})
    code, outcome = _cli(
        capsys, "apply", "--data-dir", str(data_dir), "--proposal", proposal, "--live"
    )
    assert code == maintenance_policy.EXIT_INCONCLUSIVE
    assert (outcome["measurable"], outcome["written"]) == (False, False)
    with http_client(data_dir) as http:
        refused = http.post(
            "/api/v1/cloudsync/policies/apply", json={"changes": {}, "dry_run": False}
        )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "POLICY_INCONCLUSIVE"
    assert refused.json()["detail"]["outcome"] == outcome
    assert count(data_dir, "SELECT COUNT(*) FROM machines") == 0, "the control: nothing registered"


DUPLICATE_KEY_CASES: dict[str, dict[str, Any]] = {
    "set_twice": {
        "policies": [
            {"machine_id": HUB_ID, "asset_kind": "audio", "mode": "cached"},
            {"machine_id": HUB_ID, "asset_kind": "audio", "mode": "excluded"},
        ]
    },
    "set_and_removed": {
        "policies": [{"machine_id": HUB_ID, "asset_kind": "audio", "mode": "cached"}],
        "removed_policies": [{"machine_id": HUB_ID, "asset_kind": "audio"}],
    },
}


@pytest.mark.parametrize("case", sorted(DUPLICATE_KEY_CASES))
def test_a_proposal_naming_a_cell_twice_is_refused_on_both_twins(
    case: str, fleet_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a proposal naming one cell twice is judged on one value, written as another then broken"""
    changes = DUPLICATE_KEY_CASES[case]
    rows, logged = count(fleet_dir, POLICY_ROWS), count(fleet_dir, CHANGELOG_ROWS)
    code, cli_error = _cli(
        capsys,
        "apply",
        "--data-dir",
        str(fleet_dir),
        "--proposal",
        _write(tmp_path, changes),
        "--live",
    )
    with http_client(fleet_dir) as http:
        applied = http.post(
            "/api/v1/cloudsync/policies/apply", json={"changes": changes, "dry_run": False}
        )
        validated = http.post("/api/v1/cloudsync/policies/validate", json=changes)
    assert code == maintenance_policy.EXIT_USAGE
    assert cli_error["code"] == "PROPOSAL_INVALID"
    assert applied.status_code == validated.status_code == 422, applied.text
    assert applied.json()["detail"] == validated.json()["detail"] == cli_error
    assert (count(fleet_dir, POLICY_ROWS), count(fleet_dir, CHANGELOG_ROWS)) == (rows, logged)
    assert rows == 6, "the control: the hub's cells, including audio, are live"


def test_unknown_machine_is_refused_with_exit_1(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """if set on an unregistered machine exits other than 1 or names no error code then broken"""
    code, error = _cli(
        capsys,
        "set",
        "--data-dir",
        str(fleet_dir),
        "--machine",
        "nope",
        "--kind",
        "audio",
        "--mode",
        "pinned",
        "--live",
    )
    assert code == maintenance_policy.EXIT_USAGE
    assert error["code"] == "MACHINE_NOT_FOUND"


@pytest.mark.parametrize(
    "argv",
    [
        ("policy", "set", "--data-dir", "x", "--machine", "self", "--mode", "pinned"),
        ("policy", "frobnicate", "--data-dir", "x"),
        ("policy",),
    ],
)
def test_policy_usage_errors_exit_1(argv: tuple[str, ...]) -> None:
    """if a policy usage error exits with argparse's 2 instead of the documented 1 then broken"""
    with pytest.raises(SystemExit) as excinfo:
        maintenance.main(list(argv))
    assert excinfo.value.code == maintenance_policy.EXIT_USAGE


def test_other_commands_keep_argparse_usage_exit(tmp_path: Path) -> None:
    """if the policy remap leaks into other subcommands' usage exit code then broken"""
    with pytest.raises(SystemExit) as excinfo:
        maintenance.main(["prune", "--keep-rows", "nope", "--data-dir", str(tmp_path)])
    assert excinfo.value.code == 2


def test_classes_matches_the_http_twin(fleet_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """if policy classes and GET /data-classes return different registries then broken"""
    code, cli = _cli(capsys, "classes", "--data-dir", str(fleet_dir))
    with http_client(fleet_dir) as http:
        served = http.get("/api/v1/cloudsync/data-classes")
    assert code == 0 and served.status_code == 200
    assert served.json() == cli == json.loads(json.dumps(registry_payload()))
    assert len(cli["classes"]) > 50, "the control: the registry is populated"
