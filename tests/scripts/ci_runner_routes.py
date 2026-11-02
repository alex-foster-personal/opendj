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

#: The e2e gate's own reserve (ADR-NEW-trunk-queue-drafts-use-a-reserved-e2e-pool): a
#: Trunk draft prefers the opt-in `CI_RUNS_ON_E2E_MERGE_QUEUE`, a label set on e2e hosts,
#: never the pytest-host `mq` reserve the gate has never been proven on.
E2E_MERGE_QUEUE_DISJUNCT = f"{TRUNK_DRAFT_GUARD} && vars.CI_RUNS_ON_E2E_MERGE_QUEUE"
#: The e2e gate's switch before that route, and with it.
PRE_ROUTE_E2E_GATE = "${{ fromJSON(vars.CI_RUNS_ON_E2E || '\"ubuntu-latest\"') }}"
E2E_GATE_RUNS_ON = (
    f"${{{{ fromJSON({E2E_MERGE_QUEUE_DISJUNCT} || vars.CI_RUNS_ON_E2E || '\"ubuntu-latest\"') }}}}"
)

#: The light-pool switch every routed non-pytest job read before this route existed.
PRE_ROUTE_LIGHT = "${{ fromJSON(vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
#: The same switch with the merge-queue preference in front of it.
LIGHT_RUNS_ON = (
    f"${{{{ fromJSON({MERGE_QUEUE_DISJUNCT} || vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}}}"
)

#: The two Trunk upload jobs (ADR-NEW-trunk-token-jobs-self-hosted): the light route
#: with NO hosted fallback. The Actions budget refuses hosted jobs, so an unset
#: `CI_RUNS_ON_LINUX` must fail the job loudly rather than send it back there.
TOKEN_RUNS_ON = f"${{{{ fromJSON({MERGE_QUEUE_DISJUNCT} || vars.CI_RUNS_ON_LINUX) }}}}"
#: The token route minus its merge-queue disjunct.
PRE_ROUTE_TOKEN = "${{ fromJSON(vars.CI_RUNS_ON_LINUX) }}"

#: The `quality` ratchet job (Tue 6 Oct 2026): the light chain with the opt-in
#: `CI_RUNS_ON_QUALITY` pool ahead of `CI_RUNS_ON_LINUX`, keeping its mypy cache
#: acceptance step off cold-cache megamac VMs. Unset, it is the light chain exactly.
QUALITY_RUNS_ON = (
    f"${{{{ fromJSON({MERGE_QUEUE_DISJUNCT} || vars.CI_RUNS_ON_QUALITY || "
    "vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
)
#: The `quality` route minus its merge-queue disjunct: what an unset
#: `CI_RUNS_ON_MERGE_QUEUE` resolves to.
PRE_ROUTE_QUALITY = (
    "${{ fromJSON(vars.CI_RUNS_ON_QUALITY || vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
)

_REPAIR_PR = (
    "(github.event_name == 'pull_request' && "
    "contains(github.event.pull_request.labels.*.name, 'ci:trunk-repair'))"
)
_MAIN_PUSH = "(github.event_name == 'push' && github.ref == 'refs/heads/main')"
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

#: e2e.yml's `gate` job before either e2e route, and still the `extended`
#: (nightly) job's route: the e2e pool, else hosted.
E2E_POOL_TAIL = "vars.CI_RUNS_ON_E2E || '\"ubuntu-latest\"') }}"
E2E_POOL_RUNS_ON = "${{ fromJSON(" + E2E_POOL_TAIL
#: The e2e trunk-repair reserve (ICE row INC-5,
#: ADR-NEW-e2e-trunk-repair-runner-reserve): ADR-0041's `ci:trunk-repair`
#: guard, spelled exactly as on the `test` job, in front of the e2e pool.
E2E_MAIN_FIX_DISJUNCT = f"{_REPAIR_PR} && vars.CI_RUNS_ON_E2E_MAIN_FIX"
#: The `gate` job with the reserve alone: the control an unset
#: `CI_RUNS_ON_E2E_TRUNK` must resolve to for every event.
E2E_REPAIR_RESERVE_RUNS_ON = f"${{{{ fromJSON({E2E_MAIN_FIX_DISJUNCT} || {E2E_POOL_TAIL}"
#: The e2e trunk lane (ADR-NEW-main-e2e-gets-a-trunk-runner-lane, E2ELANE-01):
#: a push to main, and nothing else, prefers the `e2e-trunk` label. Repair PRs
#: are deliberately absent: they belong to the reserve above, as on ci.yml,
#: where CI_RUNS_ON_TRUNK and CI_RUNS_ON_MAIN_FIX are separate variables.
E2E_TRUNK_DISJUNCT = f"{_MAIN_PUSH} && vars.CI_RUNS_ON_E2E_TRUNK"
#: Repair routing stays first: the merge-queue preference sits after both repair
#: disjuncts and after the main-push one, and before the general chain.
SHARD_RUNS_ON = f"{PRE_ROUTE_SHARD_HEAD}{MERGE_QUEUE_DISJUNCT} || {SHARD_TAIL}"


#: e2e.yml's `gate` (amendment Fri 9 Oct 2026): a Trunk draft prefers its own e2e reserve,
#: `CI_RUNS_ON_E2E_MERGE_QUEUE`, never the pytest-host `CI_RUNS_ON_MERGE_QUEUE`. It requires
#: `CI_RUNS_ON_E2E` too, so the gate's hosted-only steps (`vars.CI_RUNS_ON_E2E == ''`) can
#: never run on the reserve, and the reserve can never be chosen while the gate is hosted.
E2E_MERGE_QUEUE_DISJUNCT = (
    f"{TRUNK_DRAFT_GUARD} && vars.CI_RUNS_ON_E2E && vars.CI_RUNS_ON_E2E_MERGE_QUEUE"
)
#: The `gate` before the merge-queue route: trunk lane, then repair reserve, then the pool.
PRE_ROUTE_E2E_GATE = f"${{{{ fromJSON({E2E_TRUNK_DISJUNCT} || {E2E_MAIN_FIX_DISJUNCT} || {E2E_POOL_TAIL}"
#: The stacked route: as above, with the merge-queue reserve after both disjuncts and before the pool.
E2E_GATE_RUNS_ON = (
    f"${{{{ fromJSON({E2E_TRUNK_DISJUNCT} || {E2E_MAIN_FIX_DISJUNCT} || "
    f"{E2E_MERGE_QUEUE_DISJUNCT} || {E2E_POOL_TAIL}"
)


#: The main-push reserve (Mon 2 Nov 2026): a push to main prefers the opt-in
#: `CI_RUNS_ON_MAIN` pool on the five jobs main's verdict waits on (scope,
#: trunk-quarantine-list, trunk-quarantine, test, ci-gate), so main's verdict stops
#: queueing behind PR load. It sits FIRST, ahead of every other route. Unset, it is
#: falsy and each route resolves exactly as before. ci-insights joined them the same
#: day: it starts after `ci gate`, and the main-push concurrency group holds the next
#: main run pending until it completes (11 min seat wait on run 41530505387).
MAIN_RESERVE_DISJUNCT = f"{_MAIN_PUSH} && vars.CI_RUNS_ON_MAIN"
_ROUTE_HEAD = "${{ fromJSON("


def with_main_reserve(runs_on: str) -> str:
    """`runs_on` with the main-push reserve in front of its whole chain."""
    assert runs_on.startswith(_ROUTE_HEAD), runs_on
    return f"{_ROUTE_HEAD}{MAIN_RESERVE_DISJUNCT} || {runs_on[len(_ROUTE_HEAD) :]}"


def without_main_reserve(runs_on: str) -> str:
    """`runs_on` with the one main-push reserve removed: the route before it."""
    return runs_on.replace(f"{MAIN_RESERVE_DISJUNCT} || ", "", 1)


MAIN_LIGHT_RUNS_ON = with_main_reserve(LIGHT_RUNS_ON)
MAIN_TOKEN_RUNS_ON = with_main_reserve(TOKEN_RUNS_ON)
MAIN_SHARD_RUNS_ON = with_main_reserve(SHARD_RUNS_ON)
#: The ci.yml jobs that carry the main-push reserve, and the exact route each carries.
MAIN_RESERVED_JOBS = {
    "scope": MAIN_LIGHT_RUNS_ON,
    "trunk-quarantine-list": MAIN_TOKEN_RUNS_ON,
    "trunk-quarantine": MAIN_LIGHT_RUNS_ON,
    "test": MAIN_SHARD_RUNS_ON,
    "ci-gate": MAIN_LIGHT_RUNS_ON,
    "ci-insights": MAIN_TOKEN_RUNS_ON,
}




def without_merge_queue_route(runs_on: str) -> str:
    """`runs_on` with the one merge-queue disjunct removed: the pre-route expression."""
    return runs_on.replace(f"{MERGE_QUEUE_DISJUNCT} || ", "", 1)


#: runner-canary.yml's shard job (ADR-NEW-runner-canary): the budget gate's label map
#: indexed by the matrix vendor, and the matrix vendor list the gate allowed. No fallback
#: disjunct on purpose: a vendor the gate did not allow has no runner, never another one.
#: Every value the label map can hold is a vendor label from ci/runner-canary.json.
CANARY_RUNS_ON = "${{ fromJSON(needs.budget-gate.outputs.labels)[matrix.vendor] }}"
CANARY_VENDOR_MATRIX = "${{ fromJSON(needs.budget-gate.outputs.vendors) }}"
