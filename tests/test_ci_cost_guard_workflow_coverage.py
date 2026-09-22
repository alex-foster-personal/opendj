"""What the workflows actually on disk would cost, and who prices them.

Every assertion here is derived from `.github/workflows` through the readers in
tests/ci_cost_guard_workflow_reader.py, never from a table written down beside
it. A hand-maintained cost table is how E2E fell off the watch list with a
nightly job that could trip the alert, so the numbers are recomputed on every
run and the next edit to a timeout, a runner label or an event gate is checked
rather than remembered.

The unit tests of the readers themselves, and of the guard's pricing, live in
tests/test_ci_cost_guard.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.ci_cost_guard_workflow_reader import (
    GUARD,
    MACOS_DESKTOP_COMPILE,
    MACOS_NATIVE_COMPANION,
    MACOS_PACKAGING,
    WORKFLOW_DIR,
    ceiling_usd,
    e2e_ceiling_on,
    e2e_priced_events,
    guard_threshold,
    top_level_disjuncts,
    unpriced_names,
    workflow_docs,
)

# ----- the guard's own registration, derived rather than asserted ------------


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
    threshold = guard_threshold()
    workflows = workflow_docs()
    guard = yaml.safe_load(GUARD.read_text())
    triggered = set(guard[True]["workflow_run"]["workflows"])
    # The guard shares its workflow with the error sink (one observer run per
    # completion, Tue 22 Sep 2026), so the trigger list is the union of both
    # jobs' needs and the guard's gate excludes by name what only the sink
    # watches. An excluded name that is not triggered is dead text, so fail.
    unpriced, _ = unpriced_names(guard["jobs"]["assess"]["if"])
    assert unpriced <= triggered, (
        f"the guard excludes names it is not triggered by: {sorted(unpriced - triggered)}"
    )
    watched = triggered - unpriced

    can_trip = {n: c for n, d in workflows.items() if (c := ceiling_usd(d)) > threshold}
    assert can_trip, (
        "no workflow can reach the threshold, so this test would pass against an "
        "EMPTY watch list; the threshold or the pricing is wrong"
    )

    unwatched = {n: f"${c:.2f}" for n, c in can_trip.items() if n not in watched}
    assert not unwatched, (
        f"these workflows can exceed the ${threshold:.2f} alert on a single run "
        f"and nothing prices them: {unwatched}"
    )

    unknown = watched - workflows.keys()
    assert not unknown, f"the guard watches workflow names that do not exist: {sorted(unknown)}"
    unnecessary = {
        name: f"${ceiling_usd(workflows[name]):.2f}" for name in watched - can_trip.keys()
    }
    assert not unnecessary, (
        "these workflows cannot reach the alert threshold, so pricing them "
        f"adds a billed guard run without signal: {unnecessary}"
    )

    # CONTROL on the assertion above: it has to be able to fail. If every
    # workflow on disk could trip, "none unwatched" would be satisfied by a
    # list naming all of them and would prove nothing about the arithmetic.
    assert len(can_trip) < len(workflow_docs()), (
        "every workflow can trip, so this test cannot distinguish a correct "
        "list from an exhaustive one"
    )


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

    threshold = guard_threshold()
    doc = yaml.safe_load((WORKFLOW_DIR / "e2e.yml").read_text())
    # `on:` is a YAML 1.1 boolean key, and its value may be a scalar, a list or
    # a mapping. A scalar must be wrapped before it reaches set(): `on: push`
    # would otherwise decompose into {'p','u','s','h'}, and a costly job gated
    # on `github.event_name == 'push'` would be priced for four invented
    # character events and never for the real one, with this test still green.
    raw_on = doc[True] if True in doc else doc["on"]
    triggers = {raw_on} if isinstance(raw_on, str) else set(raw_on)
    priced = e2e_priced_events(condition)

    skipped = triggers - priced
    assert skipped, (
        f"the guard prices every event E2E triggers on ({sorted(triggers)}), "
        "so there is no skip left for this test to be about"
    )

    too_expensive = {
        event: f"${cost:.2f}"
        for event in sorted(skipped)
        if (cost := e2e_ceiling_on(event, doc)) > threshold
    }
    assert not too_expensive, (
        f"an E2E run on these events can exceed the ${threshold:.2f} alert and "
        f"the guard walks past it: {too_expensive}"
    )

    # CONTROL, and the reason E2E is on the watch list at all: the events the
    # guard DOES price must be able to trip it. Without this the test passes
    # against an e2e.yml whose every job is trivially cheap, where the numbers
    # are real but prove nothing about a threshold nothing can reach.
    reachable = {e: e2e_ceiling_on(e, doc) for e in sorted(priced)}
    assert any(cost > threshold for cost in reachable.values()), (
        f"no priced E2E event can reach ${threshold:.2f} ({reachable}), so this "
        "test cannot tell a correct skip from an arithmetic that never bites"
    )


# ----- the hosted-OS switch, which must not admit a branch creation ---------


@pytest.mark.parametrize(
    ("workflow_path", "job_id"),
    (
        (MACOS_DESKTOP_COMPILE, "desktop-compile"),
        (MACOS_PACKAGING, "packaging"),
        (MACOS_PACKAGING, "launcher-packaging"),
        (MACOS_NATIVE_COMPANION, "macos-native-companion"),
    ),
)
def test_enabling_hosted_os_jobs_cannot_bill_macos_minutes_for_a_branch_creation(
    workflow_path: Path, job_id: str
) -> None:
    """if the variable arm stands alone then creating any branch bills 16 macOS minutes"""
    doc = yaml.safe_load(workflow_path.read_text())
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

    condition = doc["jobs"][job_id]["if"]
    disjuncts = top_level_disjuncts(condition)

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
