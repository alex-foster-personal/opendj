"""Singular host resources must be serialized once two runners share a machine.

A hosted runner gave every job its own VM, so a job could bind a fixed port or
drive dpkg with nothing else on the box. Moving CI to self-hosted runners
retired that guarantee quietly: agentbox runs two runner services on one
machine, ci.yml and e2e.yml have independent `concurrency` groups, and nothing
in either workflow said the resources were singular. The failure that produces
is a red required check caused by SCHEDULING rather than by the change under
test, which is the worst kind, because re-running it usually goes green and
teaches everyone to re-run reds.

Codex raised the port half as P1/BLOCKING on PR #1014 (discussion_r3924582000).
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
E2E = REPO / ".github" / "workflows" / "e2e.yml"
DECKLOAD_CONFIG = REPO / "apps/webui/frontend/tests/e2e/playwright.webkit-deckload.config.ts"

#: How e2e.yml names the config on a command line.
DECKLOAD_REF = "tests/e2e/playwright.webkit-deckload.config.ts"

#: The helper that holds a named host-wide lock, and the lock these steps take.
LOCK_SCRIPT = "scripts/ci_host_lock.sh"
PORT_LOCK = "webkit-deckload"


def _deckload_steps() -> list[tuple[str, dict]]:
    """(job id, step) for every step that runs the fixed-port suite."""
    doc = yaml.safe_load(E2E.read_text())
    return [
        (job_id, step)
        for job_id, job in doc["jobs"].items()
        for step in job.get("steps") or []
        if DECKLOAD_REF in (step.get("run") or "")
    ]


def test_the_deckload_suite_really_does_pin_one_fixed_port() -> None:
    """if the port stops being fixed then the serialization below guards nothing"""
    source = DECKLOAD_CONFIG.read_text()

    assert "WEBKIT_DECKLOAD_PORT = 8690" in source, (
        "the webkit-deckload suite no longer pins port 8690, so the host lock "
        "the tests below require may be guarding a collision that cannot happen. "
        "Re-derive the contention against whatever it binds now"
    )
    assert "reuseExistingServer: false" in source, (
        "the suite now reuses an existing server, so a second run would attach "
        "rather than fail to bind. That removes the reason these steps are "
        "serialized; drop the lock deliberately rather than leaving it unexplained"
    )


def test_every_run_of_the_fixed_port_suite_holds_the_host_lock() -> None:
    """if two jobs run this suite at once then whichever binds second dies"""
    steps = _deckload_steps()

    # CONTROL: an empty list would satisfy the loop below and prove nothing. It
    # is also the likely shape of a future refactor that renames the config.
    assert len(steps) >= 2, (
        f"expected the deck-load suite to be invoked by at least two steps "
        f"(the gate quarantines a subset, the nightly runs all 23), found "
        f"{len(steps)}; this test can no longer see the steps it reasons about"
    )

    unguarded = [
        f"{job_id}: {step.get('name', '<unnamed>')}"
        for job_id, step in steps
        if LOCK_SCRIPT not in step["run"] or PORT_LOCK not in step["run"]
    ]
    assert not unguarded, (
        f"these steps bind the suite's fixed port without taking the "
        f"`{PORT_LOCK}` host lock, so a concurrent job on the same runner host "
        f"kills whichever binds second: {unguarded}"
    )


def test_the_nightly_suite_is_ordered_after_the_gate_it_shares_a_port_with() -> None:
    """if the nightly races the gate then a 45 minute wait times the gate out"""
    doc = yaml.safe_load(E2E.read_text())
    extended = doc["jobs"]["extended"]

    needs = extended.get("needs")
    needs = [needs] if isinstance(needs, str) else list(needs or [])
    assert "gate" in needs, (
        "the nightly `extended` job no longer waits for `gate`, so on a schedule "
        "or dispatch run both are eligible at once and the host lock would make "
        "one WAIT on the other. `extended` runs the full 23-test suite, so a "
        "`gate` queued behind it exhausts its own 30-minute timeout and reports "
        "a red that means nothing"
    )

    # The overshoot: `needs` alone would silently narrow this to `success()`,
    # so a red gate would take the nightly full suite down with it. Quarantined
    # tests are exactly what the nightly exists to keep visible.
    condition = str(extended.get("if", ""))
    assert "cancelled()" in condition, (
        f"`extended` gained a `needs` without keeping a !cancelled() condition, "
        f"so it now only runs when `gate` passed and the nightly full suite "
        f"disappears on the days it matters most: {condition!r}"
    )
