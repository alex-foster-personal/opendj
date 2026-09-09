"""Reading a PR's own refs (head, base, canonical main), and what stayed put.

Split out of `scripts/review_thread_triage.py` at the 600-line ceiling, on the
same seam `scripts/review_ledger.py` was split on before it: everything here
answers "what does this PR's own git state say right now", while the caller
(`fetch_pull_request`) decides what to do with the answer -- retry, refuse, or
proceed. Nothing here reads `.planning/TECH-DEBT.md` itself; that is
`review_ledger.py`'s job once these refs are in hand.

Nothing here is mocked: `_gh_graphql` shells out to the real `gh api` and a
failure is raised, never defaulted to an empty or partial result.
"""

from __future__ import annotations

import json
import subprocess

from scripts.review_ledger import OWNER, REPO, LedgerReadError
from scripts.review_thread_parse import Thread

# -----------------------------------------------------------------------------
# github
# -----------------------------------------------------------------------------


def _gh_graphql(query: str, **variables: str | int) -> dict:
    """Run one GraphQL query through `gh`, failing loudly on any error."""
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-F", f"{key}={value}"]
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    if "data" not in payload:
        raise RuntimeError(f"GraphQL response carried no data: {payload}")
    return payload["data"]


# -----------------------------------------------------------------------------
# stability guards -- the ledger is snapshotted once, and the world must not
# have moved underneath that snapshot
# -----------------------------------------------------------------------------


class HeadMovedError(RuntimeError):
    """The PR's head moved between two pages of the same review-thread fetch.

    The ledger is snapshotted once, from the FIRST page's `headRefOid`, at the
    moment a head SHA first becomes available. A force-push mid-fetch would
    silently go stale against that snapshot: removing a ledger entry could let
    the truly current head pass on the OLD head's entry, and adding one could
    read as a false DEBT-LOG NOT IN LEDGER. Neither is safe to paper over, so
    a head that moves mid-fetch fails the whole measurement, exactly like a
    failed ledger read.
    """


def _require_stable_head(number: int, expected: str, seen: str) -> None:
    """Raise if a later page's `headRefOid` disagrees with the first page's."""
    if seen != expected:
        raise HeadMovedError(
            f"PR #{number} head moved from {expected} to {seen} while paginating "
            "review threads; the ledger snapshot no longer matches this PR, re-run"
        )


class BaseRetargetedError(RuntimeError):
    """The PR's base branch changed between two reads of the same fetch.

    The ledger union is snapshotted once, from the FIRST page's `baseRefName`,
    at the same moment as the head. Retargeting a PR mid-fetch moves the second
    home of the ledger without moving the head, so nothing the head guard
    watches would notice: the gate could pass on an entry that lives only on
    the ABANDONED base, or refuse one that was just gained from the new base.
    A base that moves mid-fetch fails the whole measurement for the same reason
    a moving head does. Reported by Codex on #805, #792 round 3.
    """


def _require_stable_base(number: int, expected: str, seen: str) -> None:
    """Raise if a later read's `baseRefName` disagrees with the first page's."""
    if seen != expected:
        raise BaseRetargetedError(
            f"PR #{number} was retargeted from {expected} to {seen} while reading "
            "review threads; the ledger snapshot no longer matches this PR, re-run"
        )


#: How many times the ledger read may be retried when the branches it reads
#: move underneath it. Three, because each attempt is two extra round trips and
#: a branch that moves three times in that span is not quiet enough to measure.
_LEDGER_READ_ATTEMPTS = 3

_REFS_QUERY = """
query($owner:String!,$repo:String!,$pr:Int!,$base:String!){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$pr){ headRefOid baseRefName }
    ref(qualifiedName:$base){ target{ oid } }
    canonical: ref(qualifiedName:"refs/heads/main"){ target{ oid } }
  }
}
"""


def _fetch_pr_refs(
    number: int, base_ref: str, owner: str = OWNER, repo: str = REPO
) -> tuple[str, str, str | None, str]:
    """(head SHA, base ref name, base TIP SHA or None, canonical `main` TIP SHA).

    The base TIP is `None`, never a raise, when `base_ref` does not resolve to
    a commit at all -- most commonly a stacked PR's feature-branch base,
    deleted after merge, real history for #10 (Wed 19 Aug 2026). #805 round 9
    (Codex, discussion_r3917525195, review_thread_triage.py:267): raising here
    aborted the whole read before the head or canonical `main` ledger was ever
    reached, even though either can independently PROVE a DEBT-LOGGED claim's
    membership -- ledger membership is existential, so one readable match
    settles it. The caller decides, once every source that COULD be read has
    been (`_require_ledger_measured`), whether the gap ever mattered: only an
    unmatched claim needs the base, because absence -- unlike presence --
    needs every source checked.

    NOT `baseRefOid`. That field is the base as the PR diverged from it, not the
    branch's current tip, and this was measured rather than assumed: on #805 it
    read `e9c56755` while `main` was at `4ec60393`. Pinning to it would have
    made the defect it was meant to fix PERMANENT instead of racy -- an append
    landing on main would be invisible forever, not just to an unlucky run.
    Caught because reading the ledger at that commit made two live tests fail
    on a PR whose base predated `.planning/TECH-DEBT.md` existing at all.

    So the tip is resolved through `ref(qualifiedName:)`, using the base name
    the pages agreed on, and the PR's own `baseRefName` comes back in the same
    query so the caller can confirm the name it resolved is still the PR's base.

    #792 round 2 (Codex on #805): the page-turn check only compares CONSECUTIVE
    pages, so it never fires on a single-page PR and, on the LAST page, checks
    before that page's `_drain_comments` calls rather than after. Called once
    after `fetch_pull_request`'s loop drains, this closes that exit path.

    `main` comes back as well, and it is not redundant when the base IS main.
    `.planning/TECH-DEBT.md` documents appends pushed DIRECTLY TO MAIN, and that
    convention does not stop applying because a PR is stacked on a feature
    branch: an entry legitimately appended to main would be in neither a stacked
    PR nor its base, so the gate accused a correctly triaged PR -- and if that
    base predates the ledger, the whole measurement failed instead. Codex found
    it on #805, #792 round 5.

    All four values come from ONE query, so they are read at the same instant;
    fetching them separately would reopen the window this exists to close.
    """
    data = _gh_graphql(
        _REFS_QUERY, owner=owner, repo=repo, pr=number, base=f"refs/heads/{base_ref}"
    )["repository"]
    pr = data["pullRequest"]
    if pr is None:
        raise RuntimeError(f"PR #{number} not found in {owner}/{repo}")
    tip = data["ref"]
    base_sha = tip["target"]["oid"] if tip is not None else None
    canonical = data["canonical"]
    if canonical is None:
        # `main` itself not resolving is not the reported shape (#805 round 9
        # is about a stacked PR's own base going away, not the repo's default
        # branch), and unlike the base it is not one of two possible homes --
        # there is nothing left to prove a claim against, so this still
        # aborts outright rather than deferring to the caller.
        raise LedgerReadError(
            f"PR #{number}: refs/heads/main does not resolve to a commit, so the ledger "
            "the direct-to-main append convention writes to cannot be read"
        )
    return (
        pr["headRefOid"],
        pr["baseRefName"],
        base_sha,
        canonical["target"]["oid"],
    )


def _require_ledger_measured(
    number: int,
    base_ref: str,
    base_sha: str | None,
    head_sha: str,
    main_sha: str,
    threads: tuple[Thread, ...],
) -> None:
    """Refuse the measurement only if the base's absence actually mattered.

    #805 round 9 (Codex, discussion_r3917525195): `base_sha` is `None` when a
    stacked PR's feature-branch base has been deleted. A DEBT-LOGGED claim
    already proven present at the head or canonical `main` (`ledger_indexed`)
    needs nothing further -- one readable match settles existence regardless
    of what could not be read. Only a claim still unmatched after those reads
    (`ledger_relevant and not ledger_indexed`) is genuinely unmeasured: its
    absence cannot be confirmed without every source, and the missing base is
    exactly the source that was never checked.
    """
    if base_sha is not None:
        return
    unresolved = tuple(t for t in threads if t.ledger_relevant and not t.ledger_indexed)
    if not unresolved:
        return
    raise LedgerReadError(
        f"PR #{number}: base branch {base_ref} does not resolve to a commit (deleted?), "
        f"and {len(unresolved)} DEBT-LOGGED claim(s) are not found in the ledger at head "
        f"{head_sha} or canonical main {main_sha}; absence cannot be confirmed without "
        "the base, so this is unmeasured rather than DEBT-LOG NOT IN LEDGER"
    )
