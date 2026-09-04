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


def _matrix_runs(job_id: str, job: dict) -> int:
    """How many billed runs one job definition expands to.

    A `strategy.matrix` job bills once PER COMBINATION, so a ceiling that
    counted it once would understate the workflow by a factor of the shard
    count. Only the plain shape is read (every key a literal list, no
    `include` / `exclude`, no expression); anything else is refused rather
    than guessed, in the same fail-closed spirit as `_runner_labels`.
    """
    strategy = job.get("strategy")
    if strategy is None:
        return 1
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), (
        f"{job_id} has a strategy this test cannot read: {strategy!r}"
    )
    assert not ({"include", "exclude"} & matrix.keys()), (
        f"{job_id} uses matrix include/exclude, which this ceiling cannot count"
    )
    runs = 1
    for key, values in matrix.items():
        assert isinstance(values, list) and values, (
            f"{job_id} matrix key {key!r} is not a literal list: {values!r}"
        )
        runs *= len(values)
    return runs


def _job_ceiling_usd(job_id: str, job: dict) -> float:
    """One job's worst-case cost, priced through the guard's own SKU table.

    A matrix job is priced once per combination: the fast pytest lane shards
    across runners, and each shard is its own billed job with its own timeout.
    """
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
    return _matrix_runs(job_id, job) * (timeout + 1) * sku.rate_usd_per_minute


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

    # ONE widening, and only one: a leading `!cancelled() &&` around a
    # parenthesized disjunction is stripped. A status function decides whether
    # a run happens at all, so it can REMOVE runs and can never add an event -
    # which is the only property that makes widening a fail-closed parser safe.
    # e2e.yml's `extended` gained that guard when it was ordered after `gate`
    # (#1014). Any other unreadable clause still refuses below.
    guarded = re.fullmatch(r"!cancelled\(\) && \((.+)\)", stripped)
    if guarded:
        stripped = guarded.group(1)

    events: set[str] = set()
    # Depth-aware, so a parenthesized clause is never cut into fragments that
    # each read as a weaker claim than the clause they came from.
    for clause in _top_level_disjuncts(stripped):
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


MACOS_PACKAGING = WORKFLOW_DIR / "macos-packaging.yml"


def _top_level_disjuncts(condition: str) -> list[str]:
    """`condition` split on `||` at bracket depth zero.

    A plain `str.split("||")` would cut inside a parenthesized clause and hand
    back fragments, and a fragment reads as a weaker claim than the clause it
    came from - the same fail-open shape `_event_set` refuses above.
    """
    stripped = re.sub(r"\s+", " ", condition).strip()
    parts, depth, start = [], 0, 0
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0 and stripped[index : index + 2] == "||":
            parts.append(stripped[start:index].strip())
            index += 2
            start = index
            continue
        index += 1
    parts.append(stripped[start:].strip())
    return [part for part in parts if part]


def test_enabling_hosted_os_jobs_cannot_bill_macos_minutes_for_a_branch_creation() -> None:
    """if the variable arm stands alone then creating any branch bills 16 macOS minutes"""
    doc = yaml.safe_load(MACOS_PACKAGING.read_text())
    triggers = doc[True] if True in doc else doc["on"]

    # CONTROL: this test only guards anything because the workflow subscribes to
    # `create`, which fires on every branch and tag creation and takes no ref
    # filter. If that trigger goes away the assertions below would pass for a
    # reason that has nothing to do with the property.
    assert "create" in triggers, (
        "macos-packaging.yml no longer triggers on `create`, so a branch "
        "creation cannot reach this job and this test proves nothing. Re-derive "
        f"the risk against the triggers it actually declares: {sorted(triggers)}"
    )

    condition = doc["jobs"]["packaging"]["if"]
    disjuncts = _top_level_disjuncts(condition)

    # A bare `vars.X == 'true'` disjunct is true for EVERY event the workflow
    # triggers on, `create` included, so flipping the variable would put a
    # 16-minute macOS job (0.062 USD/min) on every branch anyone pushes.
    for clause in disjuncts:
        if "CI_HOSTED_OS_JOBS" not in clause:
            continue
        assert "github.event_name" in clause, (
            f"disjunct {clause!r} enables the hosted macOS job on the strength "
            "of the variable alone, so it admits `create` too and every branch "
            "creation would bill 16 macOS minutes. Conjoin it with the events "
            "it is meant for."
        )
        assert "'create'" not in clause, (
            f"disjunct {clause!r} names `create` explicitly, which is the event "
            "this test exists to keep off a paid runner"
        )

    # PRESENCE, not just absence: the two paths that must keep working. Without
    # these, deleting the variable arm outright would satisfy everything above
    # and silently take release cuts and manual runs down with it.
    assert any("refs/tags/v" in clause for clause in disjuncts), (
        f"a v* release cut can no longer reach the packaging gates: {condition}"
    )
    assert any("workflow_dispatch" in clause for clause in disjuncts), (
        f"the manual escape hatch is gone: {condition}"
    )

    # And the switch must still BE a switch. Deleting the variable arm outright
    # satisfies every assertion above - the loop iterates nothing - and would
    # leave a documented repo variable that turns nothing on. That overshoot is
    # the plausible over-correction to the finding this test came from, so it
    # gets its own assertion rather than being left to review.
    enabling = [clause for clause in disjuncts if "CI_HOSTED_OS_JOBS" in clause]
    assert len(enabling) == 1, (
        f"expected exactly one disjunct to read CI_HOSTED_OS_JOBS, found "
        f"{len(enabling)}; the switch documented in "
        f"docs/ci-actions-cost-review-2026-08-16.md must still enable this job: "
        f"{condition}"
    )
    assert "'push'" in enabling[0] and "'pull_request'" in enabling[0], (
        f"the variable no longer re-enables the job for push and pull_request, "
        f"which is the whole point of the switch: {enabling[0]}"
    )


def test_a_status_guarded_event_gate_is_read_and_any_other_shape_still_refuses() -> None:
    """if the widening leaks then an unreadable clause prices as a small event set"""
    guarded = (
        "!cancelled() && (github.event_name == 'schedule' "
        "|| github.event_name == 'workflow_dispatch')"
    )
    assert _event_set(guarded, "github.event_name") == {"schedule", "workflow_dispatch"}

    # The widening must not have become "skip whatever you cannot read". A ref
    # comparison admits every event the workflow triggers on, and returning a
    # small set for it is the exact fail-open this parser exists to prevent.
    with pytest.raises(AssertionError, match="cannot compute the event set"):
        _event_set("github.ref == 'refs/heads/main'", "github.event_name")

    # And a status guard around something unreadable is still unreadable.
    with pytest.raises(AssertionError, match="cannot compute the event set"):
        _event_set("!cancelled() && (github.ref == 'refs/heads/main')", "github.event_name")

