"""Bot review-thread triage gate for one pull request.

Every review thread opened by a review bot (Codex, CodeRabbit, Devin) must
reach one of three terminal states before or at merge:

    FIXED        code changed in response
    REBUTTED     reply explaining why not, thread resolved
    DEBT-LOGGED  reply "logged as tech debt: <anchor>", thread resolved,
                 entry appended to .planning/TECH-DEBT.md

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
import pathlib
import re
import subprocess
import sys

from scripts import review_coverage

from scripts.review_thread_parse import PullRequest, Thread, build_thread

# -----------------------------------------------------------------------------
# config
# -----------------------------------------------------------------------------

OWNER = "maintainer"
REPO = "music-dj-tools"

# The ledger is the other half of a DEBT-LOGGED claim. A reply naming an anchor
# that was never appended loses the finding just as silently as saying nothing.
_LEDGER = pathlib.Path(__file__).resolve().parents[1] / ".planning" / "TECH-DEBT.md"
_PERMALINK = re.compile(r"https://github\.com/\S+?#discussion_r\d+")

_THREADS_QUERY = """
query($owner:String!,$repo:String!,$pr:Int!,$cursor:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$pr){
      number title state merged url
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


def _ledger_permalinks() -> frozenset[str]:
    """Every review-thread permalink indexed in the tech-debt ledger."""
    if not _LEDGER.exists():
        return frozenset()
    return frozenset(_PERMALINK.findall(_LEDGER.read_text()))


def fetch_pull_request(number: int, owner: str = OWNER, repo: str = REPO) -> PullRequest:
    """Fetch a PR and every bot review thread on it, following pagination."""
    threads: list[Thread] = []
    ledger = _ledger_permalinks()
    cursor = ""
    head: dict = {}
    while True:
        variables: dict[str, str | int] = {"owner": owner, "repo": repo, "pr": number}
        if cursor:
            variables["cursor"] = cursor
        head = _gh_graphql(_THREADS_QUERY, **variables)["repository"]["pullRequest"]
        if head is None:
            raise RuntimeError(f"PR #{number} not found in {owner}/{repo}")
        block = head["reviewThreads"]
        for node in block["nodes"]:
            _drain_comments(node)
            thread = build_thread(node, ledger)
            if thread is not None:
                threads.append(thread)
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
    return PullRequest(
        number=head["number"],
        title=head["title"],
        state=head["state"],
        merged=head["merged"],
        url=head["url"],
        threads=tuple(threads),
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
            "DEBT-LOG NOT IN LEDGER (the reply claims it; .planning/TECH-DEBT.md does not):"
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
            "(.planning/TECH-DEBT.md) before this PR merges: a reply saying which, "
            "AND a resolve. P0/P1 and BLOCKING may not be debt-logged."
        )
    else:
        lines.append("")
        lines.append("OK: every bot review thread reached a terminal state.")
    return "\n".join(lines)


def _as_json(pr: PullRequest) -> str:
    return json.dumps(
        {
            "number": pr.number,
            "title": pr.title,
            "state": pr.state,
            "merged": pr.merged,
            "url": pr.url,
            "total": len(pr.threads),
            "failing": len(pr.failing),
            "unterminated": len(pr.unterminated),
            "violations": len(pr.violations),
            "unindexed_debt": len(pr.unindexed_debt),
            "untriaged": len(pr.silent),
            "in_progress": len(pr.in_progress),
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--owner", default=OWNER, help=f"repo owner (default {OWNER})")
    parser.add_argument("--repo", default=REPO, help=f"repo name (default {REPO})")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    pr = fetch_pull_request(args.pr, owner=args.owner, repo=args.repo)
    print(_as_json(pr) if args.json else _render(pr))

    # COVERAGE PRECONDITION. Thread triage is a silence detector, and silence
    # from a reviewer that never ran is indistinguishable from a clean review:
    # zero threads makes "every thread reached a terminal state" vacuously
    # true. #682 printed exactly that while Devin had left nothing at all. So
    # ask whether anyone looked BEFORE reporting on what they found.
    if args.json:
        # Machine consumers read the thread payload; keep its shape stable and
        # let them call review_coverage directly rather than nesting schemas.
        return 1 if pr.failing else 0

    print()
    try:
        coverage = review_coverage.triage(str(args.pr))
    except review_coverage.TriageError as exc:
        # Could not measure is not a verdict, and must not read as either one.
        print(f"[review-coverage] COULD NOT MEASURE: {exc}", file=sys.stderr)
        return 3

    return 1 if (pr.failing or coverage) else 0


if __name__ == "__main__":
    sys.exit(main())
