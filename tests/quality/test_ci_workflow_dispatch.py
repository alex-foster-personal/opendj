"""Regression contract for OPS-25 -- a fresh CI run must not require a push.

Issue #1166: `gh run rerun` replays a run's ORIGINAL resolved config, not today's, so
re-running a pre-migration run resurrects a retired fallback runner and produces a
0-step false red. Before this trigger, the only ways to force a genuinely fresh run
were a push or a close/reopen, both owner actions on someone else's branch.

[if] `ci.yml`'s `on:` block is inspected [then] it names `workflow_dispatch`
     [⛔️ if a fresh run still requires touching the branch].
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_ci_accepts_a_manual_dispatch_trigger() -> None:
    """A fresh run must be requestable without a push, close, or reopen."""
    workflow_text = (ROOT / ".github/workflows/ci.yml").read_text()
    workflow = yaml.safe_load(workflow_text)

    # PyYAML parses the bare `on:` key as the boolean True, not the string "on".
    triggers = workflow.get("on", workflow.get(True))

    assert "workflow_dispatch" in triggers, (
        "ci.yml must accept workflow_dispatch so a current-config run can be "
        "requested against any ref without a push, close, or reopen"
    )
