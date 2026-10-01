"""The exact runner-route expressions ci.yml and e2e.yml carry, named once.

Several structural pins compare `runs-on` strings byte for byte. They import the
pieces from here so the merge-queue disjunct has one spelling across all of them.

MERGE_QUEUE_DISJUNCT (ADR-NEW-trunk-queue-drafts-use-a-reserved-runner-pool):
a Trunk Merge Queue draft prefers the opt-in `CI_RUNS_ON_MERGE_QUEUE` pool. A
draft is identified by all four facts, never by its branch name alone, because
any PR author controls a branch name (the P1 raised on #4227): the event is a
pull_request, the head is `trunk-merge/*`, the author is the Trunk App (REST
login `trunk-io[bot]`), and the head lives in this repository.
"""

from __future__ import annotations

TRUNK_APP_LOGIN = "trunk-io[bot]"
TRUNK_DRAFT_GUARD = (
    "(github.event_name == 'pull_request' && "
    "startsWith(github.head_ref, 'trunk-merge/') && "
    f"github.event.pull_request.user.login == '{TRUNK_APP_LOGIN}' && "
    "github.event.pull_request.head.repo.full_name == github.repository)"
)
MERGE_QUEUE_DISJUNCT = f"{TRUNK_DRAFT_GUARD} && vars.CI_RUNS_ON_MERGE_QUEUE"

#: The light-pool switch every routed non-pytest job read before this route existed.
PRE_ROUTE_LIGHT = "${{ fromJSON(vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
#: The same switch with the merge-queue preference in front of it.
LIGHT_RUNS_ON = (
    f"${{{{ fromJSON({MERGE_QUEUE_DISJUNCT} || vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}}}"
)

_REPAIR_PR = (
    "(github.event_name == 'pull_request' && "
    "contains(github.event.pull_request.labels.*.name, 'ci:trunk-repair'))"
)
#: The `test` shard job before this route: ADR-0041 reserve, then the trunk pool
#: (ADR-NEW-trunk-ci-runs-on-agentbox-hosts-only), then #2654's general chain.
PRE_ROUTE_SHARD_HEAD = (
    f"${{{{ fromJSON({_REPAIR_PR} && vars.CI_RUNS_ON_MAIN_FIX || "
    "((github.event_name == 'push' && github.ref == 'refs/heads/main') || "
    f"{_REPAIR_PR}) && vars.CI_RUNS_ON_TRUNK || "
)
SHARD_TAIL = (
    "vars.CI_RUNS_ON_PYTEST || vars.CI_RUNS_ON_E2E || "
    "vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
)
PRE_ROUTE_SHARD = PRE_ROUTE_SHARD_HEAD + SHARD_TAIL
#: Repair routing stays first: the merge-queue preference sits after both repair
#: disjuncts and after the main-push one, and before the general chain.
SHARD_RUNS_ON = f"{PRE_ROUTE_SHARD_HEAD}{MERGE_QUEUE_DISJUNCT} || {SHARD_TAIL}"


def without_merge_queue_route(runs_on: str) -> str:
    """`runs_on` with the one merge-queue disjunct removed: the pre-route expression."""
    return runs_on.replace(f"{MERGE_QUEUE_DISJUNCT} || ", "", 1)


#: runner-canary.yml's shard job (ADR-NEW-runner-canary): the budget gate's label map
#: indexed by the matrix vendor, and the matrix vendor list the gate allowed. No fallback
#: disjunct on purpose: a vendor the gate did not allow has no runner, never another one.
#: Every value the label map can hold is a vendor label from ci/runner-canary.json.
CANARY_RUNS_ON = "${{ fromJSON(needs.budget-gate.outputs.labels)[matrix.vendor] }}"
CANARY_VENDOR_MATRIX = "${{ fromJSON(needs.budget-gate.outputs.vendors) }}"
