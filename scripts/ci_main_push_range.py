"""The range a push to main tests: from the newest main commit with a green `ci gate` to HEAD.

the maintainer, Tue 27 Oct 2026: "NEVER blast CI by running every test... on a PR, bundle or merge
lane. Full-suite runs belong to scheduled nightly jobs and to a stated hypothesis, never to
merging." Until Tue 3 Nov 2026 every push to main ran the FULL pytest lane: five shards of
about 36 minutes on the five main-only seats, 40 to 70 minutes a verdict, and merging paused
behind it (no merges 20:22 to 21:30Z that day). A main push now runs the same hard-capped
selection a PR head runs (scripts/ci_test_selection.py), over every file changed since the
last verdict that passed, plus the `CI-Tests:` lines of the PRs merged in that range. The
nightly full runs (ci.yml's schedule, full-ci.yml) still run everything and are what a
selection miss is caught by; it is fixed forward.

Why the newest GREEN verdict, not the newest completed one: a red verdict as the base would
let the next push go green without re-running what failed. Main runs are tip-only (ci.yml
`concurrency`), so most intermediate merges never get a verdict of their own; the range
covers them all.

`ci.yml`'s `scope` job runs `collect` once per main push and hands the one-line JSON to every
shard's selector as CI_MAIN_PUSH_RANGE:

    {"main_push": false}                                   not a push to refs/heads/main
    {"main_push": true, "head": H, "base": B, "scanned": n, "prs": [...],
     "declared": [...], "errors": [...]}
    {"main_push": true, "head": H, "base": null, "unknown": "why"}

The base is the newest first-parent ancestor of HEAD (HEAD itself excluded, at most
`MAX_COMMITS` back) whose `ci gate` check-run, from a CI run on main (push, schedule or
workflow_dispatch, never a pull_request run on a shared head sha), concluded SUCCESS. An
`unknown` record is not a failure: the selector then tests HEAD's own change (HEAD^1..HEAD)
inside the cap and says so.

Usage: python3 -m scripts.ci_main_push_range collect --event "$GITHUB_EVENT_PATH" \\
           --repository "$GITHUB_REPOSITORY" --out FILE      (token: GH_TOKEN, checks: read)

Exit 0 when FILE was written, an `unknown` record included. Standard library only, so the
scope job runs it with the runner's python3.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

from scripts.ci_bundle_declarations import FETCH_TIMEOUT_S, GRAPHQL_URL, BodyFetcher, Unknown, graphql_bodies
from scripts.ci_test_selection import parse_declarations

MAIN_REF = "refs/heads/main"
GATE_CHECK = "ci gate"
#: Workflow-run events whose `ci gate` is a verdict on main's own tree.
MAIN_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})
#: GitHub Actions' app id, so another app's check named `ci gate` never counts.
ACTIONS_APP_ID = 15368
#: Actions suites read per commit (13 on a main commit, Tue 3 Nov 2026) and `ci gate` runs per suite.
SUITE_PAGE = 40
RUN_PAGE = 5
#: Commits per GraphQL call; one call for all 50 exceeds the resource limit.
BATCH = 10
#: First-parent commits searched for a green verdict; about three hours of merges at 17 an hour.
MAX_COMMITS = 50
#: A merge or squash commit subject ends with its PR number: "fix: x (#8449)".
_PR_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")

#: oids -> {oid: [(event, conclusion), ...]} for the `ci gate` check-runs on each commit.
GateFetcher = Callable[[str, list[str]], dict[str, list[tuple[str, str]]]]


# ----- the first-parent chain, from the checkout ----------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def first_parent_chain(root: Path, head: str, limit: int = MAX_COMMITS) -> list[tuple[str, str]]:
    """(sha, subject) of HEAD's first-parent ancestors, newest first, HEAD itself excluded."""
    out = _git(root, "log", "--first-parent", f"--max-count={limit}", "--format=%H%x00%s", f"{head}^")
    return [tuple(line.split("\x00", 1)) for line in out.splitlines() if line]  # type: ignore[misc]


def pr_of(subject: str) -> int | None:
    match = _PR_SUBJECT.search(subject)
    return int(match[1]) if match else None


# ----- the decision, pure -----------------------------------------------------------


def green_base(chain: list[str], gates: dict[str, list[tuple[str, str]]]) -> str | None:
    """The newest commit of `chain` whose main-run `ci gate` concluded SUCCESS, or None."""
    for sha in chain:
        if any(event in MAIN_EVENTS and conclusion == "SUCCESS" for event, conclusion in gates.get(sha, [])):
            return sha
    return None


def _chain_and_base(
    root: Path, head: str, repository: str, fetch_gates: GateFetcher
) -> tuple[list[tuple[str, str]], str]:
    """HEAD's first-parent chain and its green base; Unknown when either cannot be found."""
    chain = first_parent_chain(root, head)
    if not chain:
        raise Unknown(f"{head} has no first parent in this checkout")
    shas = [sha for sha, _ in chain]
    base = green_base(shas, fetch_gates(repository, shas))
    if base is None:
        raise Unknown(f"none of the {len(chain)} first-parent commits before {head[:10]} has a green `{GATE_CHECK}`")
    return chain, base


def collect(payload: dict, root: Path, repository: str, fetch_gates: GateFetcher, fetch_bodies: BodyFetcher) -> dict:
    """The record for this event; anything unreadable is an `unknown` record, never an empty range."""
    if payload.get("ref") != MAIN_REF or not payload.get("after"):
        return {"main_push": False}
    head = str(payload["after"])
    try:
        chain, base = _chain_and_base(root, head, repository, fetch_gates)
        cut = [sha for sha, _ in chain].index(base)
        # The merges this push carries: HEAD and every first-parent commit newer than the base.
        subjects = [_git(root, "log", "-1", "--format=%s", head), *(subject for _, subject in chain[:cut])]
        prs = list(dict.fromkeys(pr for pr in map(pr_of, subjects) if pr is not None))
        declared: list[str] = []
        errors: list[str] = []
        try:
            bodies = fetch_bodies(repository, prs) if prs else {}
        except Exception as error:  # the base stands; only the optional declarations are lost, and said so
            bodies = {}
            errors.append(f"the merged PRs' bodies could not be read, so no `CI-Tests:` line joins: {error!r}")
        for pr in prs:
            paths, malformed = parse_declarations(bodies[pr].body if pr in bodies else "")
            declared += paths
            errors += [f"#{pr}: {error}" for error in malformed]
        return {
            "main_push": True,
            "head": head,
            "base": base,
            "scanned": cut + 1,
            "prs": prs,
            "declared": list(dict.fromkeys(declared)),
            "errors": errors,
        }
    except Unknown as unknown:
        return {"main_push": True, "head": head, "base": None, "unknown": str(unknown)}
    except Exception as error:  # a git, network or decode failure is UNKNOWN, loudly, below
        return {"main_push": True, "head": head, "base": None, "unknown": f"the range could not be read: {error!r}"}


# ----- fetching, one GraphQL call for the whole chain -------------------------------


def graphql_gates(token: str) -> GateFetcher:
    """One GraphQL call per `BATCH` commits, newest first, stopping after the batch holding a green gate.

    Measured live, Tue 3 Nov 2026: 50 commits with every suite and run in one query exceed
    GitHub's resource limit, and the `checkName` filter on `checkSuites` matches nothing, so the
    suites are filtered by app only and the `ci gate` run is picked per suite.
    """

    def query_batch(owner: str, name: str, oids: list[str]) -> dict[str, list[tuple[str, str]]]:
        suites = f"checkSuites(first: {SUITE_PAGE}, filterBy: {{appId: {ACTIONS_APP_ID}}})"
        runs = f'checkRuns(first: {RUN_PAGE}, filterBy: {{checkName: "{GATE_CHECK}", status: COMPLETED}})'
        fields = " ".join(
            f'c{i}: object(oid: "{oid}") {{ ... on Commit {{ {suites} {{ nodes {{ '
            f"workflowRun {{ event }} {runs} {{ nodes {{ conclusion }} }} }} }} }} }}"
            for i, oid in enumerate(oids)
        )
        query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'
        request = urllib.request.Request(
            GRAPHQL_URL,
            data=json.dumps({"query": query}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            answer = json.loads(response.read())
        if answer.get("errors"):
            raise Unknown(f"GraphQL errors: {answer['errors']}")
        found = (answer.get("data") or {}).get("repository") or {}
        return {oid: _gates(found.get(f"c{i}") or {}) for i, oid in enumerate(oids)}

    def fetch(repository: str, oids: list[str]) -> dict[str, list[tuple[str, str]]]:
        owner, name = repository.split("/", 1)
        gates: dict[str, list[tuple[str, str]]] = {}
        for start in range(0, len(oids), BATCH):
            batch = oids[start : start + BATCH]
            gates.update(query_batch(owner, name, batch))
            if green_base(batch, gates):
                break
        return gates

    return fetch


def _gates(commit: dict) -> list[tuple[str, str]]:
    gates: list[tuple[str, str]] = []
    for suite in (commit.get("checkSuites") or {}).get("nodes") or ():
        event = str((suite.get("workflowRun") or {}).get("event") or "")
        gates.extend(
            (event, str(run.get("conclusion") or "")) for run in (suite.get("checkRuns") or {}).get("nodes") or ()
        )
    return gates


# ----- CLI -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("collect", help="write this event's main-push range record")
    run.add_argument("--event", required=True, help="the GitHub event payload (GITHUB_EVENT_PATH)")
    run.add_argument("--repository", required=True, help="owner/name (GITHUB_REPOSITORY)")
    run.add_argument("--out", required=True, help="where to write the one-line JSON record")
    arguments = parser.parse_args(argv)
    payload = json.loads(Path(arguments.event).read_text(encoding="utf-8"))
    token = os.environ.get("GH_TOKEN", "")

    def no_token(_repository: str, _items: list) -> dict:
        raise Unknown("GH_TOKEN is not set, so the main verdicts cannot be read")

    record = collect(
        payload,
        Path.cwd(),
        arguments.repository,
        graphql_gates(token) if token else no_token,
        graphql_bodies(token) if token else no_token,
    )
    Path(arguments.out).write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
    if not record["main_push"]:
        print("[main-push-range] not a push to main", file=sys.stderr)
    elif record.get("unknown"):
        print(
            f"::warning title=main push range UNKNOWN::{record['unknown']}; HEAD's own change is tested",
            file=sys.stderr,
        )
    else:
        print(
            f"[main-push-range] base {record['base'][:10]} ({record['scanned']} commit(s) back), "
            f"{len(record['prs'])} PR(s), {len(record['declared'])} declared module(s)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
