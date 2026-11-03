"""The selection guard fails the whole suite, or most of it, without a written reason (DEVOPS-24).

Bundle merges (the maintainer, Tue 27 Oct 2026): no PR head, bundle or merge lane runs the whole
suite or "most" tests without a written reason. scripts/ci_selection_guard.py judges the
record scripts/ci_test_selection.py writes; ci.yml's `scope` job runs it on every pull
request, in shadow until repo variable CI_SELECTION_GUARD is `enforce`.

Regression lines:
  - [if] a FULL record with no `CI-Wide-Reason:` passes [then] the whole suite runs unexplained, so fail, [else stop].
  - [if] a narrow selection fails [then] every ordinary PR goes red, so fail, [else stop].
  - [if] an affected record with no universe size passes [then] an unmeasured share reads green, so fail, [else stop].
  - [if] ci.yml stops running the guard on the scope job's own record, or enforces it unconditionally [then] fail, [else stop].

-Claude
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.ci_selection_guard import MIN_REASON_CHARS, judge, main, written_reason

pytestmark = pytest.mark.requirement("DEVOPS-24")

ROOT = Path(__file__).resolve().parents[2]
REASON = "CI-Wide-Reason: this PR changes conftest.py, which configures every test"


def _record(mode: str, selected: int = 0, universe: int = 1708, head_ref: str = "af--feature") -> dict:
    return {
        "switch": "affected",
        "queue_switch": "affected",
        "head_ref": head_ref,
        "queue_draft": head_ref.startswith("trunk-merge/"),
        "mode": mode,
        "reason": "fell back to the full suite: global path changed" if mode == "full" else "n",
        "selected_modules": [f"tests/test_{i}.py" for i in range(selected)],
        "universe_modules": universe if mode == "affected" else 0,
    }


def _event(body: str | None = "", labels: tuple[str, ...] = ()) -> dict:
    return {"pull_request": {"body": body, "labels": [{"name": name} for name in labels]}}


# ----- the verdicts --------------------------------------------------------------


def test_a_full_suite_run_with_no_written_reason_fails():
    """Mutation target: a FULL record must fail the guard."""
    verdict = judge(_record("full"), _event("## What changed\nstuff\n"), 0.5)
    assert verdict.status == "FAIL"
    assert "CI-Wide-Reason" in verdict.lines[-1]


def test_a_scoped_run_passes():
    """Mutation target: a narrow selection must pass the guard."""
    verdict = judge(_record("affected", selected=107), _event(), 0.5)
    assert verdict.status == "PASS"


def test_a_selection_exactly_at_the_limit_passes_and_one_over_fails():
    assert judge(_record("affected", 854), _event(), 0.5).status == "PASS"
    assert judge(_record("affected", 855), _event(), 0.5).status == "FAIL"


def test_an_explicitly_empty_selection_passes():
    assert judge(_record("affected", 0), _event(), 0.5).status == "PASS"


@pytest.mark.parametrize("mode", ["full", "affected"])
def test_a_wide_or_full_run_with_a_written_reason_passes(mode):
    record = _record(mode, selected=1200)
    verdict = judge(record, _event(f"## Why\n{REASON}\n"), 0.5)
    assert verdict.status == "PASS"
    assert "conftest.py" in verdict.lines[-1]


def test_the_repair_label_is_a_written_reason():
    assert judge(_record("full"), _event(None, ("ci:trunk-repair",)), 0.5).status == "PASS"


@pytest.mark.requirement("DEVOPS-8362")
def test_a_repair_s_capped_selection_passes_without_a_wide_reason():
    """[if] a capped ci:trunk-repair selection needs `CI-Wide-Reason:` [then] the repair main waits on is blocked, so fail, [else stop]."""
    verdict = judge(_record("affected", selected=40), _event(None, ("ci:trunk-repair",)), 0.5)
    assert verdict.status == "PASS"
    assert verdict.lines[-1] == "within the limit"


def test_a_queue_draft_with_no_body_on_the_full_suite_fails():
    """A draft has no body: a full bundle is the measurement of the queue still blasting."""
    assert judge(_record("full"), {"pull_request": {"body": None}}, 0.5).status == "FAIL"


@pytest.mark.parametrize(
    "body",
    ["CI-Wide-Reason:", "CI-Wide-Reason: because", "see CI-Wide-Reason: " + "x" * 40, "ci-wide-reason: " + "x" * 40],
    ids=["empty", "too-short", "mid-line", "wrong-case"],
)
def test_a_token_reason_does_not_count(body):
    assert written_reason(body) is None
    assert judge(_record("full"), _event(body), 0.5).status == "FAIL"


def test_the_minimum_reason_is_inclusive():
    assert written_reason("CI-Wide-Reason: " + "y" * MIN_REASON_CHARS) == "y" * MIN_REASON_CHARS


@pytest.mark.parametrize("universe", [0, None, "1708"])
def test_an_affected_record_with_no_measurable_universe_is_unknown(universe):
    record = _record("affected", selected=10)
    record["universe_modules"] = universe
    assert judge(record, _event(REASON), 0.5).status == "UNKNOWN"


def test_an_unrecognized_mode_is_unknown():
    assert judge({"mode": "empty"}, _event(), 0.5).status == "UNKNOWN"


# ----- the CLI -------------------------------------------------------------------


def _write(tmp_path: Path, name: str, data: object) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("record", "status", "code"),
    [(_record("affected", 50), "PASS", 0), (_record("full"), "FAIL", 1)],
)
def test_the_cli_prints_one_verdict_and_exits_with_its_code(tmp_path, capsys, record, status, code):
    argv = ["check", "--record", _write(tmp_path, "r.json", record), "--bundle", '{"bundle":false}']
    argv += ["--event", _write(tmp_path, "e.json", _event()), "--max-share", "0.50"]
    assert main(argv) == code
    captured = capsys.readouterr()
    assert captured.out == f"{status}\n"
    assert "[selection-guard] selection:" in captured.err


def test_a_missing_record_is_unknown_never_a_pass(tmp_path, capsys):
    argv = ["check", "--record", str(tmp_path / "absent.json"), "--bundle", ""]
    argv += ["--event", _write(tmp_path, "e.json", _event()), "--max-share", "0.50"]
    assert main(argv) == 2
    assert capsys.readouterr().out == "UNKNOWN\n"


@pytest.mark.parametrize("share", ["0", "1.5", "-0.1"])
def test_a_share_outside_zero_to_one_is_refused(tmp_path, share):
    argv = ["check", "--record", "r", "--event", "e", "--bundle", "", "--max-share", share]
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 2


# ----- the ci.yml wiring ---------------------------------------------------------


def _scope_steps() -> list[dict]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    return workflow["jobs"]["scope"]["steps"]


def _guard_step() -> dict:
    steps = [step for step in _scope_steps() if step.get("id") == "selection-guard"]
    assert len(steps) == 1, "the scope job must run the selection guard exactly once"
    return steps[0]


def test_the_guard_reads_the_record_the_scope_selection_wrote():
    ids = [step.get("id") for step in _scope_steps()]
    assert ids.index("selection-guard") == ids.index("pr-selection") + 1
    selection_run = next(s for s in _scope_steps() if s.get("id") == "pr-selection")["run"]
    out_dir = "${RUNNER_TEMP:?RUNNER_TEMP must be set}/scope-pr-selection"
    assert f'--out-dir "{out_dir}"' in selection_run
    run = _guard_step()["run"]
    assert f'--record "{out_dir}/selection.json"' in run
    assert '--bundle "${CI_BUNDLE_DECLARATIONS}"' in run
    assert _guard_step()["env"]["CI_BUNDLE_DECLARATIONS"] == "${{ steps.bundle.outputs.json }}"
    assert "python3 -m scripts.ci_selection_guard check" in run
    assert "--max-share 0.50" in run


def test_the_guard_runs_on_pull_requests_in_shadow_until_enforced():
    step = _guard_step()
    assert step["if"] == "github.event_name == 'pull_request' && steps.decide.outputs.in_scope == 'true'"
    assert step["continue-on-error"] == "${{ vars.CI_SELECTION_GUARD != 'enforce' }}"
    assert step["timeout-minutes"] == 1


def test_both_selection_steps_read_the_queue_switch():
    """The scope job and the shards must agree on whether a queue draft selects."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    selection_step = next(s for s in _scope_steps() if s.get("id") == "pr-selection")
    shard_step = next(s for s in workflow["jobs"]["test"]["steps"] if s.get("id") == "pytest-shard")
    for step in (selection_step, shard_step):
        assert step["env"]["CI_QUEUE_TEST_SELECTION"] == "${{ vars.CI_QUEUE_TEST_SELECTION }}"


# ----- the nightly full suite ----------------------------------------------------


@pytest.mark.requirement("DEVOPS-23")
def test_the_full_suite_runs_nightly_on_main_and_never_on_push():
    """[if] the nightly schedule goes [then] nothing runs the whole suite once bundles select, so fail."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/full-ci.yml").read_text(encoding="utf-8"))
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert [entry["cron"] for entry in triggers["schedule"]] == ["11 3 * * *"]
    assert "push" not in triggers and "pull_request" not in triggers


def test_the_pull_request_template_examples_are_inert():
    """[if] the template's example lines parse as fields [then] an untouched body reads as a reason, so fail, [else stop]."""
    from scripts.ci_test_selection import parse_declarations

    body = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert "CI-Wide-Reason" in body and "CI-Tests" in body, "the template no longer shows the fields"
    assert written_reason(body) is None
    assert parse_declarations(body) == ([], [])


# ----- switched off, and queue bundles (Sol's P1s on PR #7598) ---------------------

DRAFT = "trunk-merge/pr-7592/0b1c"


def _bundle(prs: list[int], reasons: dict[str, str] | None = None) -> dict:
    return {"bundle": True, "prs": prs, "declared": [], "errors": [], "reasons": reasons or {}}


@pytest.mark.parametrize(
    ("field", "value", "head_ref"),
    [("switch", None, "af--feature"), ("switch", "", "af--feature"), ("queue_switch", None, DRAFT)],
)
def test_a_run_kept_full_by_a_switched_off_selection_passes(field, value, head_ref):
    """[if] an enforced guard fails runs an admin kept FULL [then] every PR or bundle blocks, so fail, [else stop]."""
    record = _record("full", head_ref=head_ref)
    record[field] = value
    bundle = _bundle([7592, 7593]) if head_ref == DRAFT else None
    verdict = judge(record, {"pull_request": {"body": None}}, 0.5, bundle)
    assert verdict.status == "PASS"
    assert "switched off" in verdict.lines[-1]


def test_a_record_with_no_switch_field_is_not_switched_off():
    """Control: an absent field is not an admin decision, so a FULL record still fails."""
    record = _record("full")
    del record["switch"]
    assert judge(record, _event(), 0.5).status == "FAIL"


def test_a_pr_heads_queue_switch_does_not_excuse_it():
    """Control: the queue switch speaks only for queue drafts."""
    record = _record("full")
    record["queue_switch"] = None
    assert judge(record, _event(), 0.5).status == "FAIL"


def test_a_full_bundle_passes_on_a_reason_one_of_its_prs_wrote():
    verdict = judge(_record("full", head_ref=DRAFT), {}, 0.5, _bundle([7592, 7593], {"7593": "changes conftest.py"}))
    assert verdict.status == "PASS"
    assert "#7593" in verdict.lines[-1]


def test_a_full_bundle_with_no_reason_from_any_pr_fails():
    assert judge(_record("full", head_ref=DRAFT), {}, 0.5, _bundle([7592, 7593])).status == "FAIL"


def test_a_wide_union_of_several_prs_passes():
    verdict = judge(_record("affected", 1200, head_ref=DRAFT), {}, 0.5, _bundle([7592, 7593, 7594]))
    assert verdict.status == "PASS"
    assert "union" in verdict.lines[-1]


def test_a_wide_bundle_of_one_pr_with_no_reason_fails():
    """Control: one PR in a bundle is that PR's own selection, and needs its own reason."""
    assert judge(_record("affected", 1200, head_ref=DRAFT), {}, 0.5, _bundle([7592])).status == "FAIL"


def test_an_unreadable_bundle_record_is_unknown():
    bundle = {"bundle": True, "unknown": "GraphQL errors"}
    assert judge(_record("full", head_ref=DRAFT), {}, 0.5, bundle).status == "UNKNOWN"


@pytest.mark.parametrize(
    "bundle",
    [None, "", [], {"bundle": True, "unknown": "unreadable"}, {"bundle": False}],
    ids=["absent", "empty-string", "list", "unknown", "disowned"],
)
@pytest.mark.parametrize("mode", ["affected", "full"])
def test_a_draft_without_a_readable_bundle_record_is_unknown_before_any_exemption(bundle, mode):
    """[if] a narrow or switched-off draft passes with no readable bundle record [then] a broken collector reads green, so fail, [else stop]."""
    record = _record(mode, 10, head_ref=DRAFT)
    record["queue_switch"] = None
    assert judge(record, {}, 0.5, bundle).status == "UNKNOWN"


def test_a_non_object_bundle_value_on_a_pr_head_is_unknown(tmp_path, capsys):
    argv = ["check", "--record", _write(tmp_path, "r.json", _record("affected", 5)), "--bundle", "[1]"]
    argv += ["--event", _write(tmp_path, "e.json", _event()), "--max-share", "0.50"]
    assert main(argv) == 2
    assert capsys.readouterr().out == "UNKNOWN\n"


def test_the_bundle_step_feeds_the_scope_selection_the_guard_and_every_shard():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    scope = workflow["jobs"]["scope"]
    ids = [step.get("id") for step in scope["steps"]]
    assert ids.index("bundle") + 1 == ids.index("pr-selection")
    bundle = scope["steps"][ids.index("bundle")]
    assert "python3 -m scripts.ci_bundle_declarations collect" in bundle["run"]
    assert bundle["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert scope["permissions"]["pull-requests"] == "read"
    assert scope["outputs"]["bundle_declarations"] == "${{ steps.bundle.outputs.json }}"
    selection = scope["steps"][ids.index("pr-selection")]
    assert selection["env"]["CI_BUNDLE_DECLARATIONS"] == "${{ steps.bundle.outputs.json }}"
    shard = next(s for s in workflow["jobs"]["test"]["steps"] if s.get("id") == "pytest-shard")
    assert shard["env"]["CI_BUNDLE_DECLARATIONS"] == "${{ needs.scope.outputs.bundle_declarations }}"


def test_a_reason_in_a_crlf_body_counts():
    """[if] a body edited in GitHub's web UI (CRLF) loses its reason [then] a real reason reads as missing, so fail, [else stop]."""
    body = "## Why\r\n" + REASON + "\r\nCI-Tests: tests/a/test_x.py -- one\r\n"
    assert written_reason(body) == REASON.split(": ", 1)[1]


def test_an_invalid_declaration_fails_even_with_a_written_reason():
    """[if] a reason excuses a declared test the lane never runs [then] the author's test silently never runs, so fail, [else stop]."""
    record = _record("full")
    record["declaration_error"] = "CI-Tests: tests/analysis/test_x.py is lane-ignored"
    verdict = judge(record, _event(REASON), 0.5)
    assert verdict.status == "FAIL"
    assert "invalid declaration" in verdict.lines[-1]


@pytest.mark.parametrize("modules", [None, "tests/test_a.py", 12, [1, 2]], ids=["absent", "string", "int", "ints"])
def test_a_record_without_a_list_of_selected_modules_is_unknown(modules):
    """[if] a malformed record counts as zero modules [then] an unmeasured share passes, so fail, [else stop]."""
    record = _record("affected", 10)
    record["selected_modules"] = modules
    assert judge(record, _event(), 0.5).status == "UNKNOWN"


def test_a_boolean_universe_is_not_a_lane_size():
    record = _record("affected", 1)
    record["universe_modules"] = True
    assert judge(record, _event(), 0.5).status == "UNKNOWN"


def test_a_pr_head_named_like_a_draft_gets_no_queue_switch_excuse():
    """Control: only an authenticated draft (record queue_draft) is excused by the queue switch."""
    record = _record("full", head_ref="trunk-merge/pr-1/mine")
    record["queue_draft"] = False
    record["queue_switch"] = None
    assert judge(record, _event(), 0.5).status == "FAIL"


@pytest.mark.parametrize(
    "bundle",
    [
        {"bundle": True, "prs": [None, None], "reasons": {}, "declared": [], "errors": []},
        {"bundle": True, "prs": [7592, 7592], "reasons": {}, "declared": [], "errors": []},
        {"bundle": True, "prs": [], "reasons": {}, "declared": [], "errors": []},
        {"bundle": True, "prs": [True, 2], "reasons": {}, "declared": [], "errors": []},
        {"bundle": True, "prs": [1, 2], "reasons": ["a reason"], "declared": [], "errors": []},
        {"bundle": True, "prs": [1, 2], "reasons": {"3": "a reason from outside"}, "declared": [], "errors": []},
        {"bundle": True, "prs": [1, 2], "reasons": {"1": ""}, "declared": [], "errors": []},
        {"bundle": True, "prs": [1, 2], "reasons": {}, "declared": "tests/a.py", "errors": []},
        {"bundle": True, "prs": [1, 2], "reasons": {}, "declared": [], "errors": None},
    ],
    ids=[
        "null-prs",
        "duplicate",
        "empty",
        "bool-pr",
        "reasons-list",
        "foreign-reason",
        "empty-reason",
        "declared-string",
        "errors-null",
    ],
)
def test_a_malformed_bundle_record_is_unknown(bundle):
    """[if] a malformed bundle record passes as a union or crashes [then] an unmeasured bundle reads green, so fail, [else stop]."""
    for record in (_record("affected", 1200, head_ref=DRAFT), _record("full", head_ref=DRAFT)):
        assert judge(record, {}, 0.5, bundle).status == "UNKNOWN"
