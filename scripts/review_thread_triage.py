"""Bot review-thread triage gate for one pull request.

Every review thread opened by a review bot (Codex, Copilot, CodeRabbit, Devin) must
reach one of three terminal states before or at merge:

    FIXED        code changed in response
    REBUTTED     reply explaining why not, thread resolved
    DEBT-LOGGED  reply "logged as tech debt: <anchor>", thread resolved,
                 entry in .planning/debt/<pr>.md on the PR branch

Each of those rows requires BOTH HALVES: a reply that records which
disposition was chosen, and a resolve that closes it. A reply alone is work in
progress ("investigating" is not a disposition). A resolve alone leaves no
record of which of the three was chosen. Silence -- an unresolved thread
nobody answered -- is never a legal state on a merged PR.

The gate additionally REFUSES a blocker written off as debt: P0/P1 and
anything marked BLOCKING must be FIXED or REBUTTED, and a debt-log reply on
one of those fails however the thread was closed.

What this script can and cannot prove. It is a SILENCE DETECTOR, not a judge
of whether a rebuttal is sound or a fix is correct. It answers "did anyone
reach a disposition here", which is mechanically checkable; it deliberately
does not try to verify that a FIXED reply's code change is real, because that
is a reviewer's judgment and a regex claiming otherwise would be worse than
no check at all.

Requirements (mini-PRD)
- `/ ` list every bot-authored review thread on a PR via the GraphQL API.
  [if a PR has bot threads and none are listed then broken]
  [if a human-authored thread is counted as a bot thread then broken]
  [if a Copilot thread (GraphQL login copilot-pull-request-reviewer) is not
  listed, as on PR #4240, then broken]
  [if pagination stops at 100 threads and drops the rest then broken]
- `/ ` classify each thread RESOLVED / RESOLVED-SILENT / IN-PROGRESS / UNTRIAGED.
  [if a replied-and-resolved thread is reported UNTRIAGED then broken]
  [if a thread whose only comments are the bot's is called triaged then broken]
  [if a bot's own follow-up comment counts as the human reply then broken]
  [if an unresolved thread carrying only "investigating" clears the gate then broken]
  [if a bare resolve with no reply clears the gate then broken]
- `/ ` refuse a blocker that was debt-logged rather than fixed or rebutted.
  [if a P1 closed with "logged as tech debt:" clears the gate then broken]
  [if a P2 NON-BLOCKING closed with a debt-log reply is refused then broken]
- `/ ` surface the P-level and BLOCKING marker per thread, across bots.
  [if a P1 badge body reports severity P? then broken]
  [if a body saying NON-BLOCKING reports BLOCKING then broken]
  [if a Devin bug marked red does not tier as P1 then broken]
  [if a Devin analysis note tiers as a defect rather than INFO then broken]
  [if a Devin summary leaks its JSON envelope instead of the headline then broken]
- `/ ` exit nonzero when any bot thread fails the gate, zero otherwise.
  [if a PR with an untriaged bot thread exits 0 then broken]
  [if a fully triaged PR exits nonzero then broken]
  [if a PR with no bot threads at all exits nonzero then broken]
- `/ ` read the tech-debt ledger from the PR's HEAD commit UNION its base
  branch, never a local checkout (#792, and #805 for the base half).
  [if a debt entry was appended directly to main under the workflow
  .planning/TECH-DEBT.md documents, the PR branch has not rebased since, and
  the gate reports NOT IN LEDGER, then broken]
  [if the base ledger cannot be read and the gate passes silently then broken]
  [if a debt entry exists only on the PR branch and the gate is run from a
  checkout without those commits, and the gate reports NOT IN LEDGER,
  then broken]
  [if a debt-log reply claims an anchor genuinely absent from the PR head's
  ledger, and the gate stays silent, then broken]
  [if the ledger cannot be read at the PR head and the gate passes silently
  or reports DEBT-LOG NOT IN LEDGER instead of COULD NOT MEASURE then broken]
  [if the PR's head moves between two pages of the same fetch and the stale
  first-page ledger snapshot is used for later pages then broken]
  [if the PR's head moves after the last page's comments have drained but
  before the verdict is built, and the verdict is stamped with the old head
  as if nothing moved, then broken]
  [if a DEBT-LOGGED claim is genuinely present at the PR's head or canonical
  main but the PR's base branch has since been deleted, and the gate reports
  COULD NOT MEASURE or DEBT-LOG NOT IN LEDGER instead of passing, then broken]
  [if a DEBT-LOGGED claim is genuinely absent from every source that could be
  read AND the base branch cannot be read, and the gate reports DEBT-LOG NOT
  IN LEDGER instead of COULD NOT MEASURE, then broken]
  [if a thread whose ledger read was skipped by design (no thread's verdict
  needed it) is rendered as DEBT-LOG NOT IN LEDGER, then broken]
  [if a reviewer resolves a thread, or edits or deletes its terminal reply,
  while the ledger's own round trips are in flight, and the verdict is still
  built from the pre-ledger-read snapshot, then broken]
- `/ ` validate the PR's OWN `.planning/debt/<pr>.md` via `review_debt_check.py`,
  reusing `scripts.debt_index`'s parser (issue #2980) instead of a copy of it.
  [if a malformed own-file passes silently, a sibling's defect is blamed on
  this PR, or a debt-file-less PR behaves any differently, then broken]

Usage. Run as a module, not as a file path: it imports its functional core
from `scripts.review_thread_parse`, and a direct path invocation puts the file
rather than the repo root on sys.path.

    python -m scripts.review_thread_triage 492
    python -m scripts.review_thread_triage 492 --json
    just review-triage 492
"""

from __future__ import annotations

import argparse
import json
import sys

try:
    from scripts import pr_scope_check, review_coverage
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.review_thread_triage") from None
    raise
from scripts.review_debt_check import check_pr_head_debt_file, render_debt_verdict
from scripts.review_ledger import OWNER, REPO, LedgerReadError, _debt_permalinks
from scripts.review_thread_parse import PullRequest, Thread, build_thread
from scripts.review_thread_refs import (
    _LEDGER_READ_ATTEMPTS,
    BaseRetargetedError,
    HeadMovedError,
    _fetch_pr_refs,
    _gh_graphql,
    _require_stable_base,
    _require_stable_head,
)

# -----------------------------------------------------------------------------
# config
# -----------------------------------------------------------------------------


_THREADS_QUERY = """
query($owner:String!,$repo:String!,$pr:Int!,$cursor:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$pr){
      number title state merged url headRefOid baseRefName
      reviewThreads(first:100, after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{
          id isResolved isOutdated path line
          comments(first:100){
            pageInfo{ hasNextPage endCursor }
            nodes{ author{login} body url createdAt }
          }
        }
      }
    }
  }
}
"""

# -----------------------------------------------------------------------------
# github
# -----------------------------------------------------------------------------

_COMMENTS_QUERY = """
query($id:ID!,$cursor:String){
  node(id:$id){
    ... on PullRequestReviewThread{
      comments(first:100, after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{ author{login} body url createdAt }
      }
    }
  }
}
"""


def _drain_comments(node: dict) -> None:
    """Top up a thread's comments in place until every page is present.

    A heavily discussed thread can push its terminal reply past the first
    page. Truncating there would report a thread as untriaged forever, which
    is the one failure mode a merge gate must not have: it would train people
    to ignore it.
    """
    block = node["comments"]
    page = block.get("pageInfo") or {}
    while page.get("hasNextPage"):
        more = _gh_graphql(_COMMENTS_QUERY, id=node["id"], cursor=page["endCursor"])
        block_next = more["node"]["comments"]
        block["nodes"] += block_next["nodes"]
        page = block_next["pageInfo"]


def _fetch_thread_nodes(
    number: int,
    expected_head: str | None,
    expected_base: str | None,
    owner: str,
    repo: str,
) -> tuple[list[dict], str, str, dict]:
    """Paginate every review thread, fully drained, returning (nodes, head_sha,
    base_ref, the last page's raw PR object).

    `expected_head`/`expected_base` are `None` on the FIRST call of a fetch,
    where this bootstraps them from the first page; every other call passes
    the already-confirmed values, and every page -- including the first -- is
    then checked against them, never just the page-to-page deltas. A caller
    that re-invokes this with its own prior result gets the same page-turn
    stability guarantee `fetch_pull_request`'s original single inline loop
    had, without duplicating the loop body.
    """
    nodes: list[dict] = []
    head_sha = expected_head
    base_ref = expected_base
    cursor = ""
    head: dict = {}
    while True:
        variables: dict[str, str | int] = {"owner": owner, "repo": repo, "pr": number}
        if cursor:
            variables["cursor"] = cursor
        head = _gh_graphql(_THREADS_QUERY, **variables)["repository"]["pullRequest"]
        if head is None:
            raise RuntimeError(f"PR #{number} not found in {owner}/{repo}")
        if head_sha is None:
            head_sha, base_ref = head["headRefOid"], head["baseRefName"]
        else:
            _require_stable_head(number, head_sha, head["headRefOid"])
            _require_stable_base(number, base_ref or "", head["baseRefName"])
        block = head["reviewThreads"]
        for node in block["nodes"]:
            _drain_comments(node)
            nodes.append(node)
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
    assert head_sha is not None and base_ref is not None  # one page always runs
    return nodes, head_sha, base_ref, head


def fetch_pull_request(number: int, owner: str = OWNER, repo: str = REPO) -> PullRequest:
    """Fetch a PR and every bot review thread on it, following pagination.

    THE LEDGER IS READ ONCE, LAST, AT PINNED COMMITS. Earlier rounds read it on
    the first page and then spent the rest of the fetch trying to notice if the
    world had moved underneath that snapshot -- first the head, then the base.
    The base guard compared a NAME, so `main` advancing was invisible to it, and
    `.planning/TECH-DEBT.md` is appended DIRECTLY TO MAIN by documented
    convention: the gate would read the ledger, someone would land the very
    entry it was about to demand, and the run would still report DEBT-LOG NOT
    IN LEDGER against a snapshot taken seconds before the row appeared. Codex
    found it on #805, #792 round 4.

    Reordering removes the precondition instead of watching for it. The pages
    are drained into raw nodes, the refs are re-read once at the end and
    checked, and only then is the ledger read -- at that revalidated head SHA
    and that revalidated base SHA, both commits rather than refs. There is no
    longer a window between the read and the check to guard, because the read
    comes after the check.

    ONLY WHEN A THREAD'S VERDICT ASKS FOR IT, recomputed fresh on every loop
    iteration below -- never decided once, outside the loop, from a snapshot
    the rest of the function goes on to invalidate. #805 round 10-continued
    (Codex, discussion_r3918773708, review_thread_triage.py:270): an earlier
    version special-cased "no thread needs the ledger" into an EARLY RETURN
    straight after the bootstrap fetch, bypassing the identity recheck and
    thread refresh below entirely -- so a force-push, retarget, or an
    edit/resolve/delete of a terminal reply during the bootstrap's own later
    pages, the exact window round 2 and round 10 closed for the ledger-needed
    path, went undetected on this one. There is now exactly one return, built
    from exactly one revalidated snapshot, whether or not that snapshot ends
    up needing a ledger read to interpret.
    """
    nodes, head_sha, base_ref, head = _fetch_thread_nodes(number, None, None, owner, repo)

    # Re-read after the loop drains, not just on the page turn: the last page's
    # `_drain_comments` calls can spend many round-trips after their own check,
    # and a single-page PR never hits the page-turn check at all.
    # READ, RE-FETCH THREADS, THEN RE-READ, THEN RETRY. Reordering removed the
    # window before the ledger read; `_ledger_permalinks` then spends its own
    # round trips AFTER that check, and the sanctioned direct-to-main append
    # lands in exactly that interval -- so an entry that became valid mid-read
    # was still reported missing. Retrying at the newer commits is right rather
    # than merely convenient: the union is MONOTONE, a later tip can only ADD
    # rows, so each attempt converges on more evidence and never less. Failing
    # outright would instead turn every append on a busy `main` into a red
    # gate, which is a self-inflicted flake. A head that moves still fails
    # immediately -- the threads themselves were fetched at the old head, so
    # no re-read can rescue them. Codex found the window on #805, #792 round 5.
    #
    # The thread re-fetch (added round 10 to close a SEPARATE staleness window
    # -- a reviewer resolving a thread, or editing/deleting its terminal reply,
    # moves neither head nor base, so nodes captured before the ledger's own
    # round trips could go stale by exactly their duration) sits INSIDE this
    # loop, before the "after" ref check, not after it. It has to: the refetch
    # is itself several round trips, and a sanctioned append landing DURING it
    # would leave `nodes` newer than the `ledger` snapshot they are about to be
    # built against -- a real thread showing a DEBT-LOGGED reply the ledger
    # genuinely now contains, checked against the STALE union that predates it,
    # rendering DEBT-LOG NOT IN LEDGER for a legally triaged claim. Codex found
    # this second window on #805, #792 round 10 (review_thread_triage.py:348).
    # Bracketing the refetch with the SAME "after" check that already guards
    # the ledger read covers both: either can only move to a newer, monotone
    # state, so the existing retry-on-mismatch loop is exactly the mechanism
    # both staleness sources need, not a separate one apiece.
    #
    # THE LOOP ALWAYS RUNS, at least once, whether or not this iteration ends
    # up reading the ledger -- see the class docstring note above and #805
    # round 10-continued. `ledger_relevant` is computed TWICE per iteration
    # (once on the nodes this iteration started with, deciding whether to
    # read; once more on the FRESHLY refetched nodes, deciding whether that
    # decision still holds) rather than trusted across the refresh: a thread
    # that only becomes -- or stops being -- DEBT-LOGGED during the refetch
    # itself is exactly the class of staleness this whole function exists to
    # close, one level down from where round 10-continued found it.
    ledger: frozenset[str] = frozenset()
    for _ in range(_LEDGER_READ_ATTEMPTS):
        fresh_head, fresh_base, base_sha, main_sha = _fetch_pr_refs(
            number, base_ref, owner=owner, repo=repo
        )
        _require_stable_head(number, head_sha, fresh_head)
        _require_stable_base(number, base_ref, fresh_base)
        # DEFER THE LEDGER READ until a thread's VERDICT actually hinges on
        # it. #805 round 7 (review_thread_triage.py:354): `disposition` never
        # reads the ledger (only `ledger_indexed` does), so it is computable
        # for free before any network round trip against
        # `.planning/TECH-DEBT.md`. round 8 (review_thread_triage.py:347): a
        # DEBT-LOGGED claim already failing for a ledger-independent reason --
        # unresolved, or illegal (P0/P1 or BLOCKING, which `illegal_debt_log`
        # fails regardless of ledger membership) -- gains nothing from the
        # read either. `Thread.ledger_relevant` is the narrower predicate:
        # RESOLVED, non-blocking DEBT-LOGGED only, the one shape whose outcome
        # is genuinely undetermined without `ledger_indexed`.
        ledger_needed = any(
            t.ledger_relevant
            for t in (build_thread(n, ledger_checked=False) for n in nodes)
            if t is not None
        )
        read_performed = ledger_needed
        if ledger_needed:
            # PINNED COMMITS, all of them. A branch NAME cannot tell you which
            # revision you read, and these branches move precisely when it
            # matters. `base_sha` is only `None` when the base branch itself
            # does not resolve (#805 round 9) -- there is then no third
            # pinned commit to hand `_ledger_permalinks`, not a ref it fails
            # to read.
            ledger = _debt_permalinks(number, head_sha, owner=owner, repo=repo)
        # `_fetch_thread_nodes` re-checks head/base stability against the
        # already-confirmed values on every one of its own pages, so a race
        # DURING this re-fetch still aborts outright rather than silently
        # winning; it is the "after" check below that catches a race in the
        # ledger the refetched threads are about to be measured against.
        nodes, head_sha, base_ref, head = _fetch_thread_nodes(
            number, head_sha, base_ref, owner, repo
        )
        after_head, after_base, after_base_sha, after_main_sha = _fetch_pr_refs(
            number, base_ref, owner=owner, repo=repo
        )
        # ALL FOUR, not just the two tips. A force-push or a retarget during
        # `_ledger_permalinks` OR the thread refetch can leave the base and
        # canonical tips untouched while the PR itself becomes a different
        # thing, and the threads in hand were fetched at the old head.
        # Identity is checked with the same two raisers as before the read,
        # deliberately: unlike a moving tip this is NOT retryable, because no
        # re-read can rescue threads already read at a head that no longer
        # exists. Codex found it on #805, round 6.
        _require_stable_head(number, head_sha, after_head)
        _require_stable_base(number, base_ref, after_base)
        # Recomputed against the FRESH nodes the refetch just produced, not
        # trusted from `ledger_needed` above -- the refetch itself can flip
        # relevance either way. #805 round 10-continued-2 (Codex,
        # discussion_r3919026022, review_thread_triage.py:372): a flip from
        # not-needed to needed means this iteration's `read_performed` is
        # False (it skipped `_ledger_permalinks`) while the FINAL nodes now
        # hold a claim that needs it -- the old code fell straight into the
        # unchanged-ref branch below and returned `ledger_checked=True`
        # against the still-empty `ledger`, rendering a genuinely logged claim
        # as DEBT-LOG NOT IN LEDGER. `continue` is the fix: loop again rather
        # than settle, so the next iteration's pre-refresh `ledger_needed`
        # (computed from these same, now-current nodes) is True and performs
        # the read for real.
        ledger_needed = any(
            t.ledger_relevant
            for t in (build_thread(n, ledger_checked=False) for n in nodes)
            if t is not None
        )
        if ledger_needed and not read_performed:
            continue
        if not ledger_needed or (after_base_sha, after_main_sha) == (base_sha, main_sha):
            break
    else:
        raise LedgerReadError(
            f"PR #{number}: the base and canonical branches moved, or the ledger's "
            f"relevance kept flipping without ever being read, during every one of "
            f"{_LEDGER_READ_ATTEMPTS} attempts to settle it, so no read covers a "
            "single instant and the answer is unmeasured rather than negative"
        )
    # `ledger_checked` mirrors whether THIS batch's read was complete: False
    # whenever no thread in the FINAL snapshot needed one at all, or the base
    # could not be resolved, so an unmatched, ledger-IRRELEVANT DEBT-LOGGED
    # claim (already failing for its own illegal/unresolved reason) does not
    # ALSO render DEBT-LOG NOT IN LEDGER against a source that was never
    # actually checked. A ledger_relevant claim left unmatched here already
    # raised via `_require_ledger_measured` below, so it never reaches this
    # construction at all.
    threads = tuple(
        t
        for t in (
            build_thread(n, ledger, ledger_checked=ledger_needed)
            for n in nodes
        )
        if t is not None
    )
    return PullRequest(
        number=head["number"],
        title=head["title"],
        state=head["state"],
        merged=head["merged"],
        url=head["url"],
        head_sha=head_sha,
        threads=threads,
        base_ref=base_ref,
    )


# -----------------------------------------------------------------------------
# reporting
# -----------------------------------------------------------------------------


def _describe(thread: Thread) -> str:
    where = f"{thread.path}:{thread.line}" if thread.line else thread.path
    flags = " OUTDATED" if thread.outdated else ""
    return (
        f"  [{thread.severity}/{thread.blocking}] {where}{flags}\n"
        f"    {thread.summary}\n"
        f"    {thread.permalink}"
    )


def _render(pr: PullRequest) -> str:
    lines = [
        f"PR #{pr.number} ({pr.state}{'/merged' if pr.merged else ''}): {pr.title}",
        f"bot review threads: {len(pr.threads)}"
        f"  resolved: {sum(1 for t in pr.threads if t.resolved)}"
        f"  IN-PROGRESS: {len(pr.in_progress)}"
        f"  RESOLVED-UNCLEAR: {len(pr.resolved_unclear)}"
        f"  RESOLVED-SILENT: {len(pr.resolved_silent)}"
        f"  UNTRIAGED: {len(pr.silent)}",
    ]
    if pr.silent:
        lines.append("")
        lines.append("UNTRIAGED (never answered at all):")
        lines += [_describe(t) for t in pr.silent]
    if pr.in_progress:
        lines.append("")
        lines.append("IN-PROGRESS (answered, but never dispositioned; resolve it or say why not):")
        lines += [_describe(t) for t in pr.in_progress]
    if pr.resolved_silent:
        lines.append("")
        lines.append("RESOLVED-SILENT (closed with no reply; which disposition was it?):")
        lines += [_describe(t) for t in pr.resolved_silent]
    if pr.resolved_unclear:
        lines.append("")
        lines.append("RESOLVED-UNCLEAR (reply names none of FIXED / REBUTTED / DEBT-LOGGED):")
        lines += [_describe(t) for t in pr.resolved_unclear]
    if pr.unindexed_debt:
        lines.append("")
        lines.append(
            "DEBT-LOG NOT IN PR DEBT FILE (the reply claims it; "
            f".planning/debt/{pr.number}.md at {pr.head_sha} does not):"
        )
        lines += [_describe(t) for t in pr.unindexed_debt]
    if pr.violations:
        lines.append("")
        lines.append("ILLEGAL DEBT-LOG (a blocker cannot be written off as debt):")
        lines += [_describe(t) for t in pr.violations]
    if pr.failing:
        lines.append("")
        lines.append(
            "Each must reach FIXED, REBUTTED, or DEBT-LOGGED "
            f"(.planning/debt/{pr.number}.md) before this PR merges: a reply saying which, "
            "AND a resolve. P0/P1 and BLOCKING may not be debt-logged."
        )
    else:
        lines.append("")
        lines.append("OK: every bot review thread reached a terminal state.")
    return "\n".join(lines)


def _as_json(
    pr: PullRequest, debt_error: str | None = None, main_side_debt: list[str] | None = None
) -> str:
    return json.dumps(
        {
            "number": pr.number,
            "title": pr.title,
            "state": pr.state,
            "merged": pr.merged,
            "url": pr.url,
            "head_sha": pr.head_sha,
            "base_ref": pr.base_ref,
            "total": len(pr.threads),
            "failing": len(pr.failing),
            "unterminated": len(pr.unterminated),
            "violations": len(pr.violations),
            "unindexed_debt": len(pr.unindexed_debt),
            "untriaged": len(pr.silent),
            "in_progress": len(pr.in_progress),
            "debt_file_error": debt_error,
            "main_side_debt_problems": main_side_debt or [],
            "threads": [
                {
                    "id": t.node_id,
                    "state": t.state,
                    "severity": t.severity,
                    "blocking": t.blocking,
                    "bot": t.bot,
                    "path": t.path,
                    "line": t.line,
                    "outdated": t.outdated,
                    "summary": t.summary,
                    "permalink": t.permalink,
                    "created_at": t.created_at,
                    "human_replies": t.human_replies,
                    "disposition": t.disposition,
                    "ledger_indexed": t.ledger_indexed,
                    "ledger_checked": t.ledger_checked,
                    "illegal_debt_log": t.illegal_debt_log,
                    "debt_not_in_ledger": t.debt_not_in_ledger,
                }
                for t in pr.threads
            ],
        },
        indent=2,
    )


# -----------------------------------------------------------------------------
# entrypoint
# -----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`argv` is `None` in normal CLI use, where `argparse` reads `sys.argv`
    itself. Accepting it explicitly lets a test drive the real entrypoint with
    real arguments instead of monkeypatching `sys.argv`, banned by AGENTS.md's
    "No mocks" contract (#805 round 10-continued, Codex, discussion_r3918584365).
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--owner", default=OWNER, help=f"repo owner (default {OWNER})")
    parser.add_argument("--repo", default=REPO, help=f"repo name (default {REPO})")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    # COVERAGE PRECONDITION. Thread triage is a silence detector, and silence
    # from a reviewer that never ran is indistinguishable from a clean review:
    # zero threads makes "every thread reached a terminal state" vacuously
    # true. #682 printed exactly that while Devin had left nothing at all. So
    # ask whether anyone looked BEFORE reporting on what they found.
    if args.json:
        # Machine consumers read the thread payload; keep its shape stable and
        # let them call review_coverage directly rather than nesting schemas.
        try:
            pr = fetch_pull_request(args.pr, owner=args.owner, repo=args.repo)
            debt_error, main_side_debt = check_pr_head_debt_file(
                pr.number, pr.head_sha, args.owner, args.repo
            )
        except (LedgerReadError, HeadMovedError, BaseRetargetedError) as exc:
            print(f"[review-thread-triage] COULD NOT MEASURE: {exc}", file=sys.stderr)
            return 3
        print(_as_json(pr, debt_error, main_side_debt))
        return 1 if (pr.failing or debt_error) else 0

    try:
        # Sample the head BEFORE coverage runs. Coverage certifies whichever
        # head it measured; the thread fetch below is a further network call
        # whose snapshot can belong to a newer, unreviewed push (PR #1053 P1
        # BLOCKING, thread r3929980333). Requiring the fetched head to equal
        # the pre-coverage sample voids the verdict on EITHER side of that
        # window, as a failed measurement, never a rendered pass.
        sampled_head = review_coverage._head_sha(str(args.pr))
        coverage = review_coverage.triage(str(args.pr))
        # Coverage reports a completed Codex round at the current head. Capture
        # threads only after that report, so a round that finishes between the
        # former snapshot and coverage collection cannot leave newly-created
        # findings outside this run's terminal-disposition check.
        pr = fetch_pull_request(args.pr, owner=args.owner, repo=args.repo)
        review_coverage._require_head_unchanged(sampled_head, pr.head_sha)
        debt_error, main_side_debt = check_pr_head_debt_file(
            pr.number, pr.head_sha, args.owner, args.repo
        )
    except review_coverage.TriageError as exc:
        # Could not measure is not a verdict, and must not read as either one.
        print(f"[review-coverage] COULD NOT MEASURE: {exc}", file=sys.stderr)
        return 3
    except (LedgerReadError, HeadMovedError, BaseRetargetedError) as exc:
        # Same rule for the thread fetch's own guards: a broken ledger read, or
        # a snapshot invalidated by a mid-fetch force-push or retarget, must not
        # render as DEBT-LOG NOT IN LEDGER, which is an accusation, not a gap.
        print(f"[review-thread-triage] COULD NOT MEASURE: {exc}", file=sys.stderr)
        return 3

    print(_render(pr))
    debt_report = render_debt_verdict(pr.number, pr.head_sha, debt_error, main_side_debt)
    if debt_report:
        print(f"\n{debt_report}")
    # OPS-41: over a declared issue scope is a failure (1); unmeasurable is 3.
    scope_rc = pr_scope_check.main([str(args.pr), "--owner", args.owner, "--repo", args.repo])
    return max(scope_rc, 1 if (pr.failing or coverage or debt_error) else 0)


if __name__ == "__main__":
    sys.exit(main())
