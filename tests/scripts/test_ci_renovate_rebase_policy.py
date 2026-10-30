"""Renovate rebases a branch only when it conflicts, never because main moved.

CICD-SPEED L02 ((private path); ADR-NEW-renovate-rebases-only-on-conflict).
Renovate PRs land through the Trunk merge queue (`renovate[bot]` is an allowed submitter in
ci/trunk-merge-queue.json), and the queue's draft re-runs `ci gate` on the merge result
(`e2e verdict` stopped gating the queue with DEVOPS-26, the maintainer, Wed 28 Oct 2026). A rebase onto a newer main therefore buys no protection, but each one is a new
PR head that pays a full `ci.yml` + `e2e.yml` run and cancels the previous one.

Measured Tue 6 Oct 17:00Z to Wed 7 Oct 17:00Z: `renovate/npm-sharp-vulnerability` (a
devDependency patch, so the automerge rule matched and Renovate's `auto` resolved to
`behind-base-branch`) got 31 PR heads carrying one identical patch (git patch-id), 1,663
runner-minutes, 564 of them in cancelled runs.

Single-line intent:
  - [if] rebaseWhen is not `conflicted` [then] automerge PRs rebase on main moves, [else stop].
  - [if] a rule or preset restores `behind-base-branch` [then] its churn returns, [else stop].
  - [if] Renovate PRs skip the merge queue [then] the premise is gone, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.requirement("INFRA-03")

ROOT = Path(__file__).resolve().parents[2]
RENOVATE = ROOT / "renovate.json"
TRUNK_QUEUE = ROOT / "ci" / "trunk-merge-queue.json"

# Values of Renovate's `rebaseWhen` that rebase a branch merely because the base moved:
# `behind-base-branch` directly, `auto` whenever the branch automerges (Renovate
# `workers/repository/update/branch/reuse.js`, determineRebaseWhenValue).
_REBASES_ON_BASE_MOVE = frozenset({"behind-base-branch", "auto"})
# Presets that set rebaseWhen=behind-base-branch (Renovate `config/presets/internal/default.js`).
_REBASE_STALE_PRESETS = frozenset({":rebaseStalePrs", "default:rebaseStalePrs"})


def _config() -> dict[str, Any]:
    return json.loads(RENOVATE.read_text(encoding="utf-8"))


def _nested_rebase_settings(node: Any, path: str = "$") -> list[tuple[str, Any]]:
    """Every `rebaseWhen` / `keepUpdatedLabel` below the top level, with its JSON path."""
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if key in {"rebaseWhen", "keepUpdatedLabel"} and path != "$":
                found.append((child, value))
            found.extend(_nested_rebase_settings(value, child))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_nested_rebase_settings(value, f"{path}[{index}]"))
    return found


def test_renovate_rebases_only_on_conflict() -> None:
    """[if] rebaseWhen isn't `conflicted` [then] automerge PRs rebase as main moves, [else stop]."""
    assert _config().get("rebaseWhen") == "conflicted"


def test_no_rule_or_preset_brings_base_move_rebases_back() -> None:
    """[if] a rule or preset restores `behind-base-branch` [then] its churn returns, [else stop].

    The top-level value is inherited by every packageRule, lockFileMaintenance and
    vulnerabilityAlerts, so this test reads every nested override. A `keepUpdatedLabel`
    is the per-PR way to force `behind-base-branch`, so it counts too.
    """
    config = _config()
    nested = _nested_rebase_settings(config)
    offenders = [
        (path, value) for path, value in nested if path.endswith(".keepUpdatedLabel") or value in _REBASES_ON_BASE_MOVE
    ]
    assert offenders == [], offenders
    assert _REBASE_STALE_PRESETS.isdisjoint(config.get("extends", []))
    # The instrument must be able to see a nested override at all (positive control).
    probe = {"packageRules": [{"rebaseWhen": "behind-base-branch"}]}
    expected = [("$.packageRules[0].rebaseWhen", "behind-base-branch")]
    assert _nested_rebase_settings(probe) == expected


def test_renovate_prs_land_through_the_merge_queue() -> None:
    """[if] Renovate PRs skip the merge queue [then] the premise is gone, [else stop].

    `conflicted` is safe only because the queue draft re-tests the merge result on
    current main. If renovate[bot] leaves the queue's submitters, revisit the ADR.
    """
    queue = json.loads(TRUNK_QUEUE.read_text(encoding="utf-8"))
    assert "renovate[bot]" in queue["allowedBotSubmitters"]
    assert "ci gate" in queue["requiredStatuses"]
