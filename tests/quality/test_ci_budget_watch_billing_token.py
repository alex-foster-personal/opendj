"""The spend ledger reads billing with the token that can read it.

ADR-NEW-ci-budget-ledger-billing-token. `ci-budget-watch.yml` has been blind
since Sat 5 Sep 2026: the ledger's one live call,
`GET /users/{owner}/settings/billing/usage`, needs a user-scoped token, and the
ledger step passed `secrets.GITHUB_TOKEN`, which can never read user billing
(#692, #4264 F1). the maintainer provisioned `CI_BILLING_READ_TOKEN` (fine-grained,
Plan: read) on Mon 28 Sep 2026; nothing read it.

The ledger step is found by the script it runs, not by its name, so a renamed
step keeps this test pointed at it.

Regression lines:
  - if the ledger step reads billing with anything but CI_BILLING_READ_TOKEN then broken
    (the spend alarm is blind again)
  - if the ledger step falls back to GITHUB_TOKEN when the billing token is unset then
    broken (a missing credential must read as BLIND, not as a quieter 403)
  - if any other step receives CI_BILLING_READ_TOKEN then broken (the user token belongs
    only to the one call that needs it)
  - if no step runs the spend ledger then broken (this test would be proving nothing)
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci-budget-watch.yml"
LEDGER_MODULE = "scripts.ci_cost_ledger"
BILLING_SECRET = "secrets.CI_BILLING_READ_TOKEN"


def _steps() -> list[tuple[str, dict]]:
    assert WORKFLOW.is_file(), "ci-budget-watch.yml is missing"
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "ci-budget-watch.yml is not a mapping"
    return [
        (f"{job_id}/{step.get('name', step.get('id', '?'))}", step)
        for job_id, job in document["jobs"].items()
        for step in job.get("steps", [])
    ]


def _ledger_step() -> tuple[str, dict]:
    ledger = [(label, step) for label, step in _steps() if LEDGER_MODULE in str(step.get("run", ""))]
    assert len(ledger) == 1, f"expected one step running {LEDGER_MODULE}, got {[label for label, _ in ledger]}"
    return ledger[0]


def test_the_ledger_reads_billing_with_the_billing_token() -> None:
    label, step = _ledger_step()
    token = str(step.get("env", {}).get("GH_TOKEN", ""))
    assert BILLING_SECRET in token, (
        f"{label} passes GH_TOKEN={token!r}; the billing usage API refuses GITHUB_TOKEN, so the spend alarm stays blind"
    )


def test_the_ledger_has_no_fallback_token() -> None:
    label, step = _ledger_step()
    token = " ".join(str(step.get("env", {}).get("GH_TOKEN", "")).split())
    assert token == "${{ " + BILLING_SECRET + " }}", (
        f"{label} passes GH_TOKEN={token!r}; an unset billing token must fail the "
        "ledger (BLIND), not fall back to a token that cannot read billing"
    )


def test_only_the_ledger_step_receives_the_billing_token() -> None:
    ledger_label, _ = _ledger_step()
    holders = [label for label, step in _steps() if BILLING_SECRET in str(step)]
    assert holders == [ledger_label], f"{BILLING_SECRET} reaches {holders}, not only {ledger_label}"
