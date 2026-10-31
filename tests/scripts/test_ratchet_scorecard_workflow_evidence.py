"""scripts/ratchet_scorecard.py: the workflow-evidence accounting a skip depends on.

Three rules, each a Codex P1 at ed60b54a0c: an aggregate's verdict is tri-state (only an
explicit SUCCESS makes a skip a deliberate out-of-scope decision), a superseded incomplete sha
contributes nothing to the withholding pass, and a workflow-definition read that fails leaves
the run's skip evidence an explicit UNKNOWN, read from the revision the run executed.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re

from scripts import ratchet_scorecard as rs

T0 = dt.datetime(2026, 10, 15, 6, 0, tzinfo=dt.UTC)
JOB = "frontend unit + check + build"
BUNDLES = "Bundle budgets (per-surface gzip ceilings)"
DRIFT = "Contract drift - TS client (armed at T0)"
DRIFT_KEY = f"{JOB}: {DRIFT}"


def _job(name: str, conclusion: str | None, jid: int, steps: list[dict] | None = None) -> dict:
    return {"id": jid, "name": name, "conclusion": conclusion, "steps": steps or []}


def _manifest(declaration: str) -> str:
    return (
        "jobs:\n"
        "  frontend:\n"
        f"    name: {JOB}\n"
        "    steps:\n"
        f"      - name: {BUNDLES}\n"
        "        run: ./budgets.sh\n"
        f"      - name: {DRIFT}\n"
        "        run: ./drift.sh\n" + declaration
    )


def _api(
    declarations: dict[str, str | None],
    calls: list[str],
    pulls: list[dict] | None = None,
    base: str = "b0",
) -> rs.Api:
    """One PR head `f0` whose frontend job failed the bundle budget and skipped the drift step
    after it, aggregated by a failed `ci gate`, with a green e2e run. `declarations` maps a
    revision (`f0` its head, `b0` its base) to the ci.yml body served there, or None for a 404;
    `pulls` is the run's own `pull_requests` field (empty on a closed run). Any other path is a
    failure, so an unexpected read — a merge ref, a commit's PRs — is loud."""
    stamp = f"{T0:%Y-%m-%dT%H:%M:%SZ}"
    field = [{"number": 7348, "base": {"sha": base}}] if pulls is None else pulls

    def run(n: int, workflow: str, conclusion: str) -> dict:
        return {
            "id": n,
            "event": "pull_request",
            "head_branch": "f",
            "head_repository": {"full_name": rs.REPO},
            "head_sha": "f0",
            "pull_requests": field,
            "conclusion": conclusion,
            "run_attempt": 1,
            "created_at": stamp,
            "updated_at": stamp,
        }

    runs = {"ci.yml": [run(1, "ci.yml", "failure")], "e2e.yml": [run(2, "e2e.yml", "success")]}
    jobs = {
        1: [
            {**_job(JOB, "failure", 11), "steps": [{"name": BUNDLES, "conclusion": "failure"},
                                                  {"name": DRIFT, "conclusion": "skipped"}]},
            _job("ci gate", "failure", 12),
        ],
        2: [_job("e2e gate", "success", 21)],
    }

    def api(path: str) -> str:
        calls.append(path)
        if content := re.fullmatch(rf"repos/{rs.REPO}/contents/\.github/workflows/ci\.yml\?ref=(\w+)", path):
            body = declarations.get(content.group(1))
            if body is None:
                raise RuntimeError("gh: HTTP 404")
            return json.dumps({"content": base64.b64encode(body.encode()).decode()})
        if listing := re.match(rf"repos/{rs.REPO}/actions/workflows/(\w+)\.yml/runs\?.*&created=(\S+)\.\.(\S+)$", path):
            lo, hi = (rs._iso(x) for x in listing.groups()[1:])
            return json.dumps(
                {"workflow_runs": [r for r in runs[f"{listing.group(1)}.yml"] if lo <= rs._iso(r["created_at"]) <= hi]}
            )
        if jobs_of := re.fullmatch(rf"repos/{rs.REPO}/actions/runs/(\d+)/attempts/\d+/jobs\?per_page=100", path):
            return json.dumps({"jobs": jobs[int(jobs_of.group(1))]})
        raise RuntimeError(f"no response for {path}")

    return api


def _collect(
    declarations: dict[str, str | None], calls: list[str], pulls: list[dict] | None = None
) -> rs.Collected:
    return rs.collect(_api(declarations, calls, pulls), T0, T0 + dt.timedelta(hours=1), workers=1)


def _sole_red(data: rs.Collected) -> bool:
    by_workflow = {h.workflow: h for h in data.heads}
    return rs._sole_red(by_workflow, frozenset().union(*(h.red for h in data.heads)))


def test_an_aggregate_verdict_is_tri_state_and_only_success_clears_a_skip() -> None:
    """`run_aggregate_verdict` reads a skipped, cancelled or absent aggregate as unknown, and a
    measured run with an unknown aggregate keeps its skipped family as withheld evidence: only
    an explicit SUCCESS proves the skip was the out-of-scope decision. Reading "not failed" as
    success let a skipped gating job's family score as cleared and a named red beside it count
    sole red."""
    skipped = rs.skipped_jobs([_job(rs.QUALITY_JOB, "skipped", 2), _job("ci gate", "skipped", 3)])
    assert skipped == frozenset({rs.QUALITY_JOB})
    for conclusion in ("skipped", "cancelled", None):
        assert rs.run_aggregate_verdict([_job("ci gate", conclusion, 3)]) == rs.AGGREGATE_UNKNOWN
    assert rs.run_aggregate_verdict([_job("ci gate", None, 3)]) == rs.AGGREGATE_UNKNOWN
    assert rs.run_aggregate_verdict([]) == rs.AGGREGATE_UNKNOWN
    assert rs.run_aggregate_verdict([_job("ci gate", "failure", 3)]) == rs.AGGREGATE_FAILURE
    assert rs.run_aggregate_verdict([_job("ci gate", "success", 3)]) == rs.AGGREGATE_SUCCESS

    def scope(aggregate: str, independent_only: bool = False) -> frozenset[str]:
        head = rs.Head("ci.yml", "b", "s1", T0, frozenset(), True, skipped=skipped, aggregate=aggregate)
        return rs._skipped_gate_scope(head, independent_only=independent_only)

    for independent_only in (False, True):
        assert scope(rs.AGGREGATE_SUCCESS, independent_only) == frozenset()
        assert scope(rs.AGGREGATE_UNKNOWN, independent_only) == frozenset({rs.QUALITY_JOB})
        assert scope(rs.AGGREGATE_FAILURE, independent_only) == frozenset({rs.QUALITY_JOB})
    # The sole-red consequence: a skip no explicit success clears holds the PR back too.
    for aggregate, sole in ((rs.AGGREGATE_SUCCESS, True), (rs.AGGREGATE_UNKNOWN, False)):
        runs = {
            "ci.yml": rs.Head("ci.yml", "b", "s1", T0, frozenset({DRIFT_KEY}), True,
                              skipped=skipped, aggregate=aggregate),
            "e2e.yml": rs.Head("e2e.yml", "b", "s1", T0, frozenset()),
        }
        assert rs._sole_red(runs, frozenset({DRIFT_KEY})) is sole, aggregate


def test_a_superseded_incomplete_sha_withholds_no_family() -> None:
    """`_unread_shas` drops a non-newest sha whose missed runs were all cancelled, so its
    evidence contributes nothing; the measured-head withholding pass must exclude it too, or a
    gate reads UNKNOWN on the very evidence the superseded rule discards."""
    old = [
        rs.Head("ci.yml", "b", "old", T0, frozenset(), False, cancelled=True,
                skipped=frozenset({rs.QUALITY_JOB}), created=T0),
        rs.Head("e2e.yml", "b", "old", T0, frozenset(), True, created=T0),
    ]
    newer = [
        rs.Head(w, "b", "new", T0 + dt.timedelta(hours=2), frozenset(), True, created=T0 + dt.timedelta(hours=2))
        for w in rs.WORKFLOWS
    ]
    shas = rs._by_branch_sha(old + newer)["b"]
    assert rs._unread_shas(shas, None) == []
    assert rs.unread_gate_scope(old + newer, None)("quality:complexity") == 0
    # Control: the same incomplete sha as the branch's newest one is unread and does scope it.
    assert rs.unread_gate_scope(old, None)("quality:complexity") == 1


def test_step_guards_are_proven_from_both_parents_of_the_merge() -> None:
    """A pull_request run executes a merge of its head into its base, and GitHub publishes
    neither that merge commit for the run nor a ref bound to it (the merge ref is the latest
    simulated merge, `merge_commit_sha` the eventual integration), so neither is read. A step
    both parents declare with no `if:` is unguarded in the merge; one they disagree on proves
    nothing, so the skip stays independent withheld evidence and the head is not sole red."""
    calls: list[str] = []
    data = _collect({"f0": _manifest(""), "b0": _manifest("")}, calls)
    assert [c for c in calls if "/contents/" in c] == [
        f"repos/{rs.REPO}/contents/.github/workflows/ci.yml?ref=f0",
        f"repos/{rs.REPO}/contents/.github/workflows/ci.yml?ref=b0",
    ]
    assert not [c for c in calls if "git/ref" in c or re.search(r"/pulls/\d+$", c)], calls
    ci = {h.workflow: h for h in data.heads}["ci.yml"]
    assert ci.skipped == frozenset({DRIFT_KEY}) and ci.skipped_behind_failure == frozenset({DRIFT_KEY})
    assert _sole_red(data) is True
    # The base guards the step: the merge's declaration is unknown, so nothing is attributed.
    guarded = _collect({"f0": _manifest(""), "b0": _manifest("        if: always()\n")}, [])
    assert guarded.heads[0].skipped_behind_failure == frozenset()
    assert _sole_red(guarded) is False


def test_an_unreadable_workflow_leaves_the_run_unmeasured_and_counted() -> None:
    """A failed, unparseable or missing workflow definition proves nothing about a post-failure
    skip, so the run's skip evidence is UNKNOWN: the head loses its full verdict and the count
    is surfaced, rather than every skip silently reading as independent evidence."""
    data = _collect({"m0": None}, [])
    ci = {h.workflow: h for h in data.heads}["ci.yml"]
    assert (ci.measured, ci.guard_unread, ci.skipped_behind_failure) == (False, True, frozenset())
    assert data.guard_reads_unread == 1
    window = (T0, T0 + dt.timedelta(hours=1))
    assert (rs.owned_guard_unreads(data.heads, window), rs.unmeasured_heads(data.heads, window)) == (1, 1)
    assert rs.incomplete_heads(data.heads, window) == 1
    assert _sole_red(data) is False


def test_only_a_skip_that_withholds_a_gate_costs_a_guard_read() -> None:
    """The read exists to prove whether a gate-withholding skip was the failure's shadow. An
    ancillary step skipped after a failure (an upload), or any skip of a job that gates
    nothing, cannot move sole-red or scope a gate, so it must not cost a read — nor turn the
    run unmeasured when that read would have failed."""
    ancillary = {
        **_job(JOB, "failure", 11),
        "steps": [{"name": BUNDLES, "conclusion": "failure"},
                  {"name": "Upload frontend unit test report", "conclusion": "skipped"}],
    }
    ungated = {
        **_job("Trunk e2e root report", "failure", 13),
        "steps": [{"name": "Report", "conclusion": "failure"}, {"name": "Upload", "conclusion": "skipped"}],
    }
    gate_withholding = {
        **_job(JOB, "failure", 12),
        "steps": [{"name": BUNDLES, "conclusion": "failure"}, {"name": DRIFT, "conclusion": "skipped"}],
    }
    assert rs._skips_after_a_failure(ancillary) is False
    assert rs._skips_after_a_failure(ungated) is False
    assert rs._skips_after_a_failure(gate_withholding) is True


def test_a_closed_run_proves_nothing_rather_than_guessing_its_base() -> None:
    """A closed run's own `pull_requests` field is empty, and the recovered PR only carries the
    base as it stands today — not the revision this run merged into. So nothing is proven for
    it: the skips stay independent withheld evidence (no false sole-red) while the reds the run
    did measure still count. A payload naming two PRs cannot name the run's base at all, which
    is an unproven read and is counted."""
    calls: list[str] = []
    data = _collect({"f0": _manifest(""), "b0": _manifest("")}, calls, pulls=[])
    ci = {h.workflow: h for h in data.heads}["ci.yml"]
    assert (ci.measured, ci.guard_unread, ci.skipped_behind_failure) == (True, False, frozenset())
    assert ci.red == frozenset({f"{JOB}: {BUNDLES}"})
    assert (data.guard_reads_unread, _sole_red(data)) == (0, False)
    # The same run's own array naming two PRs: no single base, so the proof is unproven.
    two = _collect(
        {"f0": _manifest(""), "b0": _manifest("")}, [],
        pulls=[{"number": 7348, "base": {"sha": "b0"}}, {"number": 9, "base": {"sha": "b9"}}],
    )
    assert (two.guard_reads_unread, _sole_red(two)) == (1, False)


def _shadow_api(
    base_declaration: str | None, calls: list[str], head_declaration: str | None = None
) -> rs.Api:
    """The collector's own shape for the merge-parent proof: one PR head `f0` whose base is
    `b0`, its frontend job failing the bundle budget and skipping the drift step after it, with
    a failed `ci gate` as the aggregate and a green e2e run. `base_declaration` is ci.yml at
    `b0` and `head_declaration` at `f0` (None for a 404), so a test can disagree the parents."""
    stamp = f"{T0:%Y-%m-%dT%H:%M:%SZ}"

    def run(n: int, workflow: str, conclusion: str) -> dict:
        return {
            "id": n,
            "event": "pull_request",
            "head_branch": "f",
            "head_repository": {"full_name": rs.REPO},
            "head_sha": "f0",
            "pull_requests": [{"number": 7348, "base": {"sha": "b0"}}],
            "conclusion": conclusion,
            "run_attempt": 1,
            "created_at": stamp,
            "updated_at": stamp,
        }

    runs = {"ci.yml": [run(1, "ci.yml", "failure")], "e2e.yml": [run(2, "e2e.yml", "success")]}
    jobs = {
        1: [
            {**_job(JOB, "failure", 11), "steps": [{"name": BUNDLES, "conclusion": "failure"},
                                                  {"name": DRIFT, "conclusion": "skipped"}]},
            _job("ci gate", "failure", 12),
        ],
        2: [_job("e2e gate", "success", 21)],
    }

    def api(path: str) -> str:
        calls.append(path)
        if content := re.fullmatch(rf"repos/{rs.REPO}/contents/\.github/workflows/ci\.yml\?ref=(\w+)", path):
            body = base_declaration if content.group(1) == "b0" else head_declaration
            if body is None:
                raise RuntimeError("gh: HTTP 404")
            return json.dumps({"content": base64.b64encode(body.encode()).decode()})
        if listing := re.match(rf"repos/{rs.REPO}/actions/workflows/(\w+)\.yml/runs\?.*&created=(\S+)\.\.(\S+)$", path):
            lo, hi = (rs._iso(x) for x in listing.groups()[1:])
            return json.dumps(
                {"workflow_runs": [r for r in runs[f"{listing.group(1)}.yml"] if lo <= rs._iso(r["created_at"]) <= hi]}
            )
        if jobs_of := re.fullmatch(rf"repos/{rs.REPO}/actions/runs/(\d+)/attempts/\d+/jobs\?per_page=100", path):
            return json.dumps({"jobs": jobs[int(jobs_of.group(1))]})
        raise RuntimeError(f"no response for {path}")

    return api


def test_the_shadow_evidence_is_the_declaration_both_parents_agree_on() -> None:
    """collect() must hand `skips_behind_a_failure` the declaration the run's merge kept, which
    is the one both parents agree on — never a single revision's. Proven unguarded in both, the
    drift step is the failure's shadow and the head is ratchet-only; an unreadable parent proves
    nothing, so the run is unmeasured and counted, never a silent independent skip."""
    window = (T0, T0 + dt.timedelta(hours=1))
    calls: list[str] = []
    proven = rs.collect(_shadow_api(_manifest(""), calls, head_declaration=_manifest("")), *window, workers=1)
    assert [c for c in calls if "/contents/" in c] == [
        f"repos/{rs.REPO}/contents/.github/workflows/ci.yml?ref=f0",
        f"repos/{rs.REPO}/contents/.github/workflows/ci.yml?ref=b0",
    ]
    (ci,) = [h for h in proven.heads if h.workflow == "ci.yml"]
    (e2e,) = [h for h in proven.heads if h.workflow == "e2e.yml"]
    assert ci.skipped == frozenset({DRIFT_KEY}) and ci.skipped_behind_failure == frozenset({DRIFT_KEY})
    assert ci.aggregate == rs.AGGREGATE_FAILURE and ci.measured and not ci.guard_unread
    assert rs._sole_red({"ci.yml": ci, "e2e.yml": e2e}, ci.red) is True
    assert rs.head_summary([ci, e2e], window) == (1, 1)
    # The base cannot be read: the merge's declaration is unknown, so the run is UNKNOWN.
    unread = rs.collect(_shadow_api(None, [], head_declaration=_manifest("")), *window, workers=1)
    by_workflow = {h.workflow: h for h in unread.heads}
    ci_unread = by_workflow["ci.yml"]
    assert ci_unread.skipped == frozenset({DRIFT_KEY}) and ci_unread.skipped_behind_failure == frozenset()
    assert (ci_unread.measured, ci_unread.guard_unread, unread.guard_reads_unread) == (False, True, 1)
    assert rs.owned_guard_unreads(unread.heads, window) == 1
    assert rs._sole_red({"ci.yml": ci_unread, "e2e.yml": by_workflow["e2e.yml"]}, ci_unread.red) is False
