import json
import re
from pathlib import Path

import pytest
import yaml

from scripts.ci_cost_guard import infer_standard_sku, price_jobs, render_markdown

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
GUARD = WORKFLOW_DIR / "ci-cost-guard.yml"


def _job(name, labels, start, end, *, steps=None):
    return {
        "name": name,
        "labels": labels,
        "started_at": start,
        "completed_at": end,
        "conclusion": "success",
        "html_url": f"https://github.test/jobs/{name}",
        "steps": steps or [],
    }


def test_prices_parallel_jobs_by_rounded_job_minutes_not_workflow_wall_time():
    jobs = [
        _job("linux", ["ubuntu-latest"], "2026-01-01T00:00:00Z", "2026-01-01T00:01:01Z"),
        _job("windows", ["windows-latest"], "2026-01-01T00:00:00Z", "2026-01-01T00:01:01Z"),
        _job("mac", ["macos-latest"], "2026-01-01T00:00:00Z", "2026-01-01T00:01:01Z"),
    ]

    result = price_jobs(jobs)

    assert result["total_cost"] == (2 * 0.006) + (2 * 0.010) + (2 * 0.062)
    assert [row["billed_minutes"] for row in result["jobs"]] == [2, 2, 2]


def test_unknown_hosted_runner_is_unpriced_instead_of_assigned_a_cheap_rate():
    job = _job(
        "future-large-runner",
        ["custom-128-core"],
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:02:00Z",
    )

    result = price_jobs([job])

    assert result["total_cost"] == 0
    assert result["jobs"] == []
    assert result["unknown_jobs"][0]["name"] == "future-large-runner"


def test_self_hosted_runner_has_zero_github_runner_charge():
    assert infer_standard_sku(["self-hosted", "linux"]).rate_usd_per_minute == 0


def test_started_subsecond_job_is_never_underpriced_as_zero_minutes():
    job = _job(
        "instant",
        ["ubuntu-latest"],
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
    )

    result = price_jobs([job])

    assert result["jobs"][0]["billed_minutes"] == 1
    assert result["total_cost"] == 0.006


def test_report_flags_strictly_greater_than_one_dollar_and_lists_steps():
    step = {
        "name": "test",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T03:00:00Z",
        "conclusion": "failure",
    }
    job = _job(
        "runaway",
        ["ubuntu-latest"],
        "2026-01-01T00:00:00Z",
        "2026-01-01T03:00:00Z",
        steps=[step],
    )

    report, result = render_markdown(
        workflow_name="CI",
        run_url="https://github.test/runs/42",
        run_id="42",
        threshold=1.0,
        jobs=[job],
    )

    assert result["total_cost"] == 1.08
    assert result["over_threshold"] is True
    assert "**$1.080**" in report
    assert "runaway" in report
    assert "test" in report


# ----- the guard's own registration, derived rather than asserted ------------


def _workflows() -> dict[str, dict]:
    """Every workflow definition on disk, keyed by the `name:` the guard sees.

    Both suffixes GitHub will execute, not just `.yml`: Codex pointed out on
    #713 that a `.yaml` workflow whose ceiling exceeds the threshold stays
    absent from the watch list while the repository-wide assertion below still
    passes, because the glob never loads it.
    """

    out: dict[str, dict] = {}
    seen: dict[str, Path] = {}
    for path in sorted(p for pat in ("*.yml", "*.yaml") for p in WORKFLOW_DIR.glob(pat)):
        doc = yaml.safe_load(path.read_text())
        name = doc.get("name", path.stem)
        # Refuse a collision rather than let the later file win. GitHub happily
        # runs two workflows sharing a `name:`, and this dict keys on the name
        # the guard sees, so a silent overwrite prices only one of them: an
        # expensive definition can be hidden behind a cheap namesake and the
        # shared name left off the watch list with everything still green.
        # Reachable because the glob above deliberately reads BOTH suffixes.
        assert name not in seen, (
            f"{path.name} and {seen[name].name} both declare `name: {name}`, so "
            "pricing one of them silently drops the other. Rename one, or price "
            "every document per name."
        )
        seen[name] = path
        out[name] = doc
    return out


def _ceiling_usd(doc: dict) -> float:
    """Worst-case cost of one run: EVERY job at its own timeout.

    Every job, including ones behind an `if`, because a ceiling that ignores a
    conditional job is exactly the mistake this test exists to catch: the
    guard's comment priced E2E at its 30-minute gate and missed the 45-minute
    nightly `extended` job sitting beside it.

    Priced through the production `infer_standard_sku`, so a runner the guard
    could not price is a runner this cannot price either, and the missing
    number fails the test instead of quietly reading as cheap.
    """
    return sum(_job_ceiling_usd(i, j) for i, j in (doc.get("jobs") or {}).items())


#: The runner-switch expression `runs-on` carries since CI moved to self-hosted
#: runners: `${{ fromJSON(vars.CI_RUNS_ON_X || '"ubuntu-latest"') }}`. Only this
#: exact shape is read; see `_runner_labels`.
_RUNNER_SWITCH = re.compile(
    r"\$\{\{\s*fromJSON\(\s*vars\.[A-Z0-9_]+\s*\|\|\s*'(?P<fallback>.+?)'\s*\)\s*\}\}"
)


def _runner_labels(job_id: str, runs_on: object) -> list[str]:
    """The labels to price `runs-on` at, resolving the runner-switch expression.

    `runs-on` stopped being a literal label when runner selection became a repo
    variable, so the same workflow file bills at two different rates depending
    on a value that is not in the repository. The ceiling has to be the WORST
    case the switch can select, and the only candidate this file can read is
    the literal fallback baked into the `||`.

    That is also the expensive one, which is why resolving to it is safe rather
    than convenient: `infer_standard_sku` prices self-hosted at $0, so pricing
    the hosted fallback leaves every ceiling exactly where it stood before the
    switch existed and cannot drop a workflow off the watch list. A variable
    pointing at some OTHER hosted SKU is priced at RUN time by `price_jobs`,
    which reads the labels the API reports and never sees this expression.

    Fail-closed on anything else, for the same reason `_event_set` refuses a
    clause it cannot read: an expression resolved by guesswork could name a
    cheap runner for an expensive job and the missing alert would look green.
    """
    if isinstance(runs_on, str):
        labels = [runs_on]
    elif isinstance(runs_on, list):
        labels = [str(label) for label in runs_on]
    elif runs_on is None:
        labels = []
    else:
        raise AssertionError(
            f"{job_id} has a runs-on of type {type(runs_on).__name__}, which is "
            "neither a label, a list of labels nor absent, so it cannot be priced"
        )
    if not any("${{" in label for label in labels):
        return labels
    assert len(labels) == 1, (
        f"{job_id} mixes an expression with literal labels ({labels}), so which "
        "runner it selects cannot be read here"
    )
    match = _RUNNER_SWITCH.fullmatch(labels[0].strip())
    assert match, (
        f"{job_id} runs on the expression {labels[0]!r}, which this test cannot "
        "price. Only the `fromJSON(vars.X || '<json>')` runner switch is "
        "understood; widen this deliberately rather than letting an unreadable "
        "expression be priced by guesswork."
    )
    fallback = json.loads(match.group("fallback"))
    return [fallback] if isinstance(fallback, str) else list(fallback)


def _job_ceiling_usd(job_id: str, job: dict) -> float:
    """One job's worst-case cost, priced through the guard's own SKU table."""
    assert "strategy" not in job, (
        f"{job_id} has a matrix; one job no longer means one billed run "
        "and this ceiling would understate it"
    )
    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int), (
        f"{job_id} has no explicit timeout-minutes, so its ceiling is "
        "GitHub's 360-minute default and this arithmetic is meaningless"
    )
    labels = _runner_labels(job_id, job.get("runs-on"))
    sku = infer_standard_sku(labels)
    assert sku is not None, f"{job_id} runs on {labels}, which has no known rate"
    # One billed minute MORE than the timeout, because that is what production
    # would charge. `price_jobs` bills `max(1, math.ceil(seconds / 60))`, so a
    # job cancelled at a 30-minute timeout whose recorded duration runs even
    # fractionally past the mark bills 31 minutes, not 30. Treating the timeout
    # as an exact billed ceiling understates every workflow by up to one minute,
    # and a workflow sitting exactly ON the threshold is then classified as
    # unable to trip and dropped from the watch list. Codex found this on #713:
    # Windows Parity priced at exactly $0.30 against a `> $0.30` alert.
    return (timeout + 1) * sku.rate_usd_per_minute


def test_a_runner_switch_is_priced_at_the_hosted_fallback_it_can_select() -> None:
    """The ceiling has to survive the variable being unset, which is hosted.

    Self-hosted is $0 in the SKU table, so resolving the switch the other way
    would silently zero every Linux ceiling on disk and drop workflows off the
    watch list without a single assertion going red - the exact fail-open this
    module exists to prevent.
    """
    switch = "${{ fromJSON(vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"

    assert _runner_labels("test", switch) == ["ubuntu-latest"]
    sku = infer_standard_sku(_runner_labels("test", switch))
    assert sku is not None, "the hosted fallback must price through the SKU table"
    assert sku.rate_usd_per_minute > 0


def test_a_runner_switch_falling_back_to_a_label_list_keeps_every_label() -> None:
    """The fallback is JSON, and GitHub accepts a list there as well as a string."""
    switch = "${{ fromJSON(vars.CI_RUNS_ON_E2E || '[\"macos-latest\",\"large\"]') }}"

    assert _runner_labels("gate", switch) == ["macos-latest", "large"]


def test_a_runs_on_expression_this_test_cannot_read_is_refused_not_guessed() -> None:
    """Fail-closed, like `_event_set`: an unpriced runner must redden, not vanish.

    A `runs-on` resolved by guesswork could name self-hosted ($0) for a job
    that really runs on macOS, and the workflow would leave the watch list with
    every assertion still green.
    """
    for unreadable in (
        "${{ vars.CI_RUNS_ON_LINUX }}",
        "${{ fromJSON(inputs.runner) }}",
        "${{ fromJSON(vars.CI_RUNS_ON_LINUX) }}",
    ):
        with pytest.raises(AssertionError, match="cannot"):
            _runner_labels("test", unreadable)


def test_a_literal_runs_on_is_still_read_exactly_as_written() -> None:
    """CONTROL: the resolver must not start rewriting runners that need no help."""
    assert _runner_labels("test", "ubuntu-latest") == ["ubuntu-latest"]
    assert _runner_labels("test", ["self-hosted", "linux"]) == ["self-hosted", "linux"]


def _guard_threshold() -> float:
    match = re.search(r"--threshold\s+([0-9.]+)", GUARD.read_text())
    assert match, "the guard no longer passes --threshold; this test cannot measure"
    return float(match.group(1))


def test_every_workflow_that_can_trip_the_guard_is_watched() -> None:
    """The registration list is a claim about arithmetic, so check the arithmetic.

    A workflow whose own timeouts cap it below the threshold can never alert,
    and pricing it costs a billed minute per run for no signal - which is why
    workflows get removed from the list. The failure mode is removing one that
    CAN trip, and it has already happened once: E2E was dropped on a hand-written
    table that counted one of its two jobs, so a nightly run with a $0.45 ceiling
    lost its per-run alert. Codex caught it on #713 by reading the workflow.

    Nothing here trusts that table. Each ceiling is recomputed from the workflow
    files through the guard's own pricing, so the next edit to a timeout, a
    runner label, or a job list is checked rather than remembered.
    """
    threshold = _guard_threshold()
    watched = set(yaml.safe_load(GUARD.read_text())[True]["workflow_run"]["workflows"])

    can_trip = {n: c for n, d in _workflows().items() if (c := _ceiling_usd(d)) > threshold}
    assert can_trip, (
        "no workflow can reach the threshold, so this test would pass against an "
        "EMPTY watch list; the threshold or the pricing is wrong"
    )

    unwatched = {n: f"${c:.2f}" for n, c in can_trip.items() if n not in watched}
    assert not unwatched, (
        f"these workflows can exceed the ${threshold:.2f} alert on a single run "
        f"and nothing prices them: {unwatched}"
    )

    # CONTROL on the assertion above: it has to be able to fail. If every
    # workflow on disk could trip, "none unwatched" would be satisfied by a
    # list naming all of them and would prove nothing about the arithmetic.
    assert len(can_trip) < len(_workflows()), (
        "every workflow can trip, so this test cannot distinguish a correct "
        "list from an exhaustive one"
    )


def _event_set(condition: str, variable: str) -> set[str]:
    """The event names `condition` admits. Fail-closed: EVERY disjunct counts.

    Only one shape is accepted: a disjunction in which every clause is
    `<variable> == \'<name>\'`. An earlier revision skipped past clauses that
    did not mention `variable`, and Codex called that on #713: a disjunct like
    `github.ref == \'refs/heads/main\'` admits every event the workflow
    triggers on, including `push`, while the parser silently returned
    `{\'schedule\'}` and the containment below still passed. A clause this
    function cannot read may widen the set arbitrarily, so it must refuse
    rather than skip - an uncomputable set returned as a small one satisfies
    every containment, which is the failure it exists to prevent.
    """
    stripped = re.sub(r"\s+", " ", condition).strip()
    events: set[str] = set()
    for clause in (c.strip() for c in stripped.split("||")):
        match = re.fullmatch(rf"{re.escape(variable)} == \'([A-Za-z_]+)\'", clause)
        assert match, (
            f"cannot compute the event set: clause {clause!r} is not a "
            f"`{variable} == \'<name>\'` comparison, so the events it admits "
            f"are unknown and may be all of them. Widen the parser "
            f"deliberately, or gate the job on event names only."
        )
        events.add(match.group(1))
    return events


def _e2e_priced_events(condition: str) -> set[str]:
    """The events the guard prices FOR E2E, read from its `assess` gate.

    The gate opens with `github.event.workflow_run.name != \'E2E\'`, which is
    the escape hatch for every other workflow and is what makes the remaining
    disjuncts E2E-specific. That one clause is matched exactly and removed;
    everything after it goes through the strict parser above, so a fourth
    disjunct of any other shape reddens this rather than being skipped.
    """
    stripped = re.sub(r"\s+", " ", condition).strip()
    escape = "github.event.workflow_run.name != \'E2E\'"
    head, sep, tail = stripped.partition("||")
    assert head.strip() == escape and sep, (
        f"the guard\'s gate no longer opens with {escape!r}, so which of its "
        f"clauses are E2E-specific can no longer be read: {condition}"
    )
    return _event_set(tail, "github.event.workflow_run.event")


def _e2e_ceiling_on(event: str, doc: dict) -> float:
    """What an E2E run triggered by `event` can cost at worst.

    Every job that would run on that event, priced. A job with no `if` runs on
    every trigger the workflow declares; a gated one runs only on the events
    its condition admits, read by the fail-closed parser above.
    """
    total = 0.0
    for job_id, job in doc["jobs"].items():
        gate = job.get("if")
        if gate is None or event in _event_set(gate, "github.event_name"):
            total += _job_ceiling_usd(job_id, job)
    return total


def test_every_e2e_run_the_guard_skips_is_below_the_alert_threshold() -> None:
    """The property the E2E skip actually rests on, not a proxy for it.

    The guard prices E2E only on schedule and workflow_dispatch. That is safe
    if and only if an E2E run on any OTHER trigger cannot reach the alert
    threshold - so compute exactly that, per event, from the jobs that would
    run on it. Codex pointed out on #713 that checking the shape of
    `extended`'s gate leaves two ways to break the arithmetic without moving
    the gate at all: raise the always-on `gate` job's timeout, or add a second
    ungated job. Both are priced here.
    """
    condition = yaml.safe_load(GUARD.read_text())["jobs"]["assess"]["if"]
    assert "E2E" in condition, (
        "the guard's gate no longer special-cases E2E, so which events it "
        "prices can no longer be read here. Skipping would be fail-open: a "
        "gate generalized to event names only would leave costly push and "
        "pull_request runs unpriced while this test reports green. Re-derive "
        f"the arithmetic against the new gate instead: {condition}"
    )

    threshold = _guard_threshold()
    doc = yaml.safe_load((WORKFLOW_DIR / "e2e.yml").read_text())
    # `on:` is a YAML 1.1 boolean key, and its value may be a scalar, a list or
    # a mapping. A scalar must be wrapped before it reaches set(): `on: push`
    # would otherwise decompose into {'p','u','s','h'}, and a costly job gated
    # on `github.event_name == 'push'` would be priced for four invented
    # character events and never for the real one, with this test still green.
    raw_on = doc[True] if True in doc else doc["on"]
    triggers = {raw_on} if isinstance(raw_on, str) else set(raw_on)
    priced = _e2e_priced_events(condition)

    skipped = triggers - priced
    assert skipped, (
        f"the guard prices every event E2E triggers on ({sorted(triggers)}), "
        "so there is no skip left for this test to be about"
    )

    too_expensive = {
        event: f"${cost:.2f}"
        for event in sorted(skipped)
        if (cost := _e2e_ceiling_on(event, doc)) > threshold
    }
    assert not too_expensive, (
        f"an E2E run on these events can exceed the ${threshold:.2f} alert and "
        f"the guard walks past it: {too_expensive}"
    )

    # CONTROL, and the reason E2E is on the watch list at all: the events the
    # guard DOES price must be able to trip it. Without this the test passes
    # against an e2e.yml whose every job is trivially cheap, where the numbers
    # are real but prove nothing about a threshold nothing can reach.
    reachable = {e: _e2e_ceiling_on(e, doc) for e in sorted(priced)}
    assert any(cost > threshold for cost in reachable.values()), (
        f"no priced E2E event can reach ${threshold:.2f} ({reachable}), so this "
        "test cannot tell a correct skip from an arithmetic that never bites"
    )
