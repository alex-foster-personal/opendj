"""Surface a pull request's measured scope against its issue's declared limits.

OPS-41 acceptance line 3 (issue #3352): when a branch's measured commits or files
exceed the limits its issue declares, the scope check fails before review and
prints both measurements. #3352 was a one-commit fix whose PR carried 118
commits and 131 files, inherited from an unpublished shared checkout.

An issue declares limits with one body line, for example:

    Scope limit: commits=5 files=20

Linked issues are the PR's closing references plus any `Refs #N` in its body.
With several declarations the tightest limit on each axis wins.

    python -m scripts.pr_scope_check 1234            # print the verdict
    python -m scripts.pr_scope_check 1234 --comment  # also post it on the PR

Exit codes: 0 within the declared limits, or no limit declared (printed as
UNDECLARED, never as OK); 1 over a declared limit; 3 could not measure, which
includes a `Scope limit:` line that does not parse. Counts
come from GitHub's own PR fields (commits in head not in base, changed files),
the same set a reviewer is shown.

What could satisfy this check without satisfying its intent: a PR whose issue
declares nothing. That case prints UNDECLARED with the measurements, and
OVERSIZED when it is past the fleet review bound below, so it is visible on the
PR rather than read as a pass. The bound does not fail the check, because no
issue asked for it.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
from collections.abc import Callable

from scripts.review_ledger import OWNER, REPO
from scripts.worker_worktree_guard import Scope, ScopeError, assert_scope

# Fleet review bound for a PR whose issue declares no limit. It only labels the
# PR OVERSIZED; it never fails the check (see module docstring).
FLEET_REVIEW_MAX_COMMITS: int = 40
FLEET_REVIEW_MAX_FILES: int = 80

_LIMIT_RE = re.compile(
    r"(?im)^\s*scope[ -]limits?\s*:\s*commits\s*=\s*(\d+)\s*[,;]?\s*files\s*=\s*(\d+)"
)
# A keyword then a LIST of issues: "Refs #1, #2, and #3" links all three, whatever mix of
# whitespace, commas, "&", "/" and "and" separates them. Reading only the
# first would turn a limit declared on #2 into UNDECLARED, a partial read as no verdict.
_REFS_RE = re.compile(
    r"(?i)\b(?:refs?|close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(#\d+(?:(?:[\s,&/]|\band\b)+#\d+)*)"
)
_ISSUE_NUMBER_RE = re.compile(r"#(\d+)")
# Any line that starts like a declaration. One that _LIMIT_RE cannot parse is an error,
# because reading it as no declaration would print UNDECLARED for a PR that declared one.
_LIMIT_LINE_RE = re.compile(r"(?im)^\s*scope[ -]limits?\s*:.*$")

_PR_QUERY = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){
pullRequest(number:$n){
headRefOid body commits{totalCount} changedFiles additions deletions
closingIssuesReferences(first:50){totalCount nodes{number body}}}}}"""
# issueOrPullRequest, because `Refs #N` may name a pull request, which declares no scope.
_REF_QUERY = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){
issueOrPullRequest(number:$n){__typename ... on Issue{number body}}}}"""


class MeasureError(RuntimeError):
    """The PR or its issues could not be read."""


@dataclasses.dataclass(frozen=True)
class Limit:
    commits: int
    files: int
    issues: tuple[int, ...]


def declared_limit(issue_bodies: dict[int, str]) -> Limit | None:
    """The tightest `Scope limit:` line across every linked issue body, or None if none has one.

    Every line counts, not the first per body: an issue that states a limit twice is held
    to the tighter one, the same rule as across issues.
    """
    for number, body in sorted(issue_bodies.items()):
        for line in _LIMIT_LINE_RE.findall(body or ""):
            if not _LIMIT_RE.match(line):
                raise MeasureError(
                    f"#{number} has a Scope limit line that does not parse: {line.strip()!r}; "
                    "write it as `Scope limit: commits=N files=M`"
                )
    found: list[tuple[int, int, int]] = [
        (number, int(match.group(1)), int(match.group(2)))
        for number, body in sorted(issue_bodies.items())
        for match in _LIMIT_RE.finditer(body or "")
    ]
    if not found:
        return None
    return Limit(
        commits=min(c for _, c, _ in found),
        files=min(f for _, _, f in found),
        issues=tuple(sorted({n for n, _, _ in found})),
    )


def referenced_issues(body: str) -> set[int]:
    """Every issue number named by a Refs/Fixes/Closes/Resolves clause, lists included."""
    return {
        int(number)
        for clause in _REFS_RE.findall(body)
        for number in _ISSUE_NUMBER_RE.findall(clause)
    }


def verdict(scope: Scope, limit: Limit | None) -> tuple[int, str]:
    measured = (
        f"commits={scope.commits}, files={scope.files}, "
        f"additions={scope.additions}, deletions={scope.deletions}"
    )
    if limit is None:
        oversized = scope.commits > FLEET_REVIEW_MAX_COMMITS or scope.files > FLEET_REVIEW_MAX_FILES
        tag = (
            (
                f" OVERSIZED (fleet review bound commits={FLEET_REVIEW_MAX_COMMITS}, "
                f"files={FLEET_REVIEW_MAX_FILES})"
            )
            if oversized
            else ""
        )
        return 0, f"[pr-scope] UNDECLARED: no linked issue declares 'Scope limit:'; {measured}{tag}"
    source = ", ".join(f"#{n}" for n in limit.issues)
    try:
        assert_scope(scope, max_commits=limit.commits, max_files=limit.files)
    except ScopeError as exc:
        return 1, f"[pr-scope] FAIL: {exc} (declared by {source})"
    return 0, (
        f"[pr-scope] OK: commits={scope.commits} (max {limit.commits}), "
        f"files={scope.files} (max {limit.files}) declared by {source}"
    )


def _gh_graphql(query: str, **variables: object) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    process = subprocess.run(args, capture_output=True, text=True, check=False)
    if process.returncode != 0:
        raise MeasureError(f"gh api graphql failed: {process.stderr.strip()[:300]}")
    return json.loads(process.stdout)["data"]["repository"]


def fetch(
    number: int, owner: str, repo: str, graphql: Callable[..., dict] = _gh_graphql
) -> tuple[Scope, dict[int, str]]:
    pr = graphql(_PR_QUERY, o=owner, r=repo, n=number)["pullRequest"]
    if pr is None:
        raise MeasureError(f"PR #{number} not found in {owner}/{repo}")
    closing = pr["closingIssuesReferences"]
    if closing["totalCount"] > len(closing["nodes"]):
        # A limit on an unread issue would read as UNDECLARED, so a partial read is no verdict.
        raise MeasureError(
            f"PR #{number} links {closing['totalCount']} closing issues, "
            f"read {len(closing['nodes'])}"
        )
    bodies = {i["number"]: i["body"] for i in closing["nodes"]}
    for ref in sorted(referenced_issues(pr["body"] or "") - set(bodies)):
        node = graphql(_REF_QUERY, o=owner, r=repo, n=ref)["issueOrPullRequest"]
        kind = node["__typename"] if node is not None else None
        if kind == "Issue":
            bodies[node["number"]] = node["body"]
        elif kind == "PullRequest":
            continue
        elif kind is None:
            raise MeasureError(f"PR #{number} body references #{ref}, which does not resolve")
        else:
            raise MeasureError(f"#{ref} resolved to unexpected type {kind}")
    scope = Scope(pr["commits"]["totalCount"], pr["changedFiles"], pr["additions"], pr["deletions"])
    return scope, bodies


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("pr", type=int)
    parser.add_argument("--owner", default=OWNER)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--comment", action="store_true", help="also post the verdict on the PR")
    args = parser.parse_args(argv)
    try:
        scope, bodies = fetch(args.pr, args.owner, args.repo)
        limit = declared_limit(bodies)
    except (MeasureError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"[pr-scope] COULD NOT MEASURE: {exc}", file=sys.stderr)
        return 3
    rc, line = verdict(scope, limit)
    print(line)
    if args.comment:
        posted = subprocess.run(
            [
                "gh",
                "pr",
                "comment",
                str(args.pr),
                "--repo",
                f"{args.owner}/{args.repo}",
                "--body",
                f"{line}\n\n<!-- pr-scope-check v1 -->",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if posted.returncode != 0:
            print(f"[pr-scope] COULD NOT POST: {posted.stderr.strip()[:300]}", file=sys.stderr)
            return 3
    return rc


if __name__ == "__main__":
    sys.exit(main())
