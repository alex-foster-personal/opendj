"""The `CI-Tests:` and `CI-Wide-Reason:` lines of every PR a Trunk queue draft tests.

A Trunk draft has no author body: Trunk writes a banner that links the PRs in the batch and
the PRs ahead of it whose changes the draft's tree also carries ("depends on the changes
from"). Bundle merges ((private path)) run one selected set per bundle, so the
declarations those PRs made at their own heads must reach the bundle run: their `CI-Tests:`
modules join the bundle's selection, and their `CI-Wide-Reason:` lines are the bundle's
written reasons for the selection guard. Found by Sol's review of PR #7598 (two P1s).

`ci.yml`'s `scope` job runs `collect` once per pull request and hands the JSON to the
selector (the scope job's own step and every shard, as CI_BUNDLE_DECLARATIONS) and to the
guard, so the bodies are fetched once, in one GraphQL call, not once per shard.

The JSON, one line:

    {"bundle": false}                                    not a Trunk draft
    {"bundle": true, "prs": [...], "declared": [...], "errors": [...], "reasons": {"4562": "..."}}
    {"bundle": true, "unknown": "why the bodies could not be read"}

A draft is Trunk's only by `scripts.ci_test_selection.is_queue_draft`, the one classifier
the selector uses too: head ref `trunk-merge/pr-N/...` or `trunk-temp/pr-N/...`, author
`trunk-io[bot]`, head repository this one. A branch name alone is caller-controlled.

Usage: python3 -m scripts.ci_bundle_declarations collect --event "$GITHUB_EVENT_PATH" \\
           --repository "$GITHUB_REPOSITORY" --out FILE      (token: GH_TOKEN)

Exit 0 when FILE was written, including an `unknown` record; nonzero only when FILE could not
be written. Standard library only, so the scope job runs it with the runner's python3.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_selection_guard import written_reason
from scripts.ci_test_selection import is_queue_draft, parse_declarations

GRAPHQL_URL = "https://api.github.com/graphql"
#: GraphQL's page ceiling; a PR with more labels than one page is Unknown, never partial.
LABEL_PAGE = 100
FETCH_TIMEOUT_S = 30

REPAIR_LABEL = "ci:trunk-repair"


@dataclass(frozen=True)
class Member:
    """What the bundle needs from one PR it carries: its body and its labels."""

    body: str
    labels: tuple[str, ...] = ()


#: Fetches {pr number: Member} for one repository; injectable so tests never touch the network.
BodyFetcher = Callable[[str, list[int]], dict[int, Member]]


class Unknown(Exception):
    """The bodies the bundle needs could not be read; the reason says why."""


# ----- which PRs a draft carries ---------------------------------------------------


def bundle_prs(payload: dict, repository: str) -> list[int] | None:
    """Every PR a Trunk draft's tree carries, or None when the event is not a Trunk draft."""
    if not is_queue_draft(payload, repository):
        return None
    pull_request = payload.get("pull_request") or {}
    link = re.compile(rf"github\.com/{re.escape(repository)}/pull/(\d+)")
    numbers = list(dict.fromkeys(int(n) for n in link.findall(str(pull_request.get("body") or ""))))
    own = pull_request.get("number")
    numbers = [n for n in numbers if n != own]
    if not numbers:
        raise Unknown(f"Trunk draft #{own} links no pull request of {repository} in its body")
    return numbers


def summarize(bodies: dict[int, Member], prs: list[int]) -> dict:
    """The bundle record: the union of declared modules and each PR's written reason.

    A PR's `ci:trunk-repair` label is its written reason, as at its own head (Codex, PR #7598):
    a repair that passed its head guard on the label must not fail the bundle's.
    """
    missing = [pr for pr in prs if pr not in bodies]
    if missing:
        raise Unknown(f"no body returned for PR(s) {', '.join(f'#{pr}' for pr in missing)}")
    declared: list[str] = []
    errors: list[str] = []
    reasons: dict[str, str] = {}
    for pr in prs:
        paths, malformed = parse_declarations(bodies[pr].body)
        declared += paths
        errors += [f"#{pr}: {error}" for error in malformed]
        reason = written_reason(bodies[pr].body)
        if reason:
            reasons[str(pr)] = reason
        elif REPAIR_LABEL in bodies[pr].labels:
            reasons[str(pr)] = f"the `{REPAIR_LABEL}` label"
    return {
        "bundle": True,
        "prs": prs,
        "declared": list(dict.fromkeys(declared)),
        "errors": errors,
        "reasons": reasons,
    }


# ----- fetching, one GraphQL call for the whole bundle -----------------------------


def graphql_bodies(token: str) -> BodyFetcher:
    def fetch(repository: str, prs: list[int]) -> dict[int, Member]:
        owner, name = repository.split("/", 1)
        fields = " ".join(
            f"p{pr}: pullRequest(number: {pr}) {{ body labels(first: {LABEL_PAGE}) {{ nodes {{ name }} pageInfo {{ hasNextPage }} }} }}"
            for pr in prs
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
        missing = [pr for pr in prs if not found.get(f"p{pr}")]
        if missing:  # a null alias can come back with no top-level error (Sol, PR #7598)
            raise Unknown(f"GraphQL returned no pull request for {', '.join(f'#{pr}' for pr in missing)}")
        return {pr: _member(found[f"p{pr}"]) for pr in prs}

    return fetch


def _member(node: dict) -> Member:
    """A PR's body and labels; a label list GitHub paginated past one page is Unknown.

    A truncated list could hide `ci:trunk-repair`, the PR's written reason, so it is never
    read as complete (review P1 on PR #7598).
    """
    labels_node = node.get("labels") or {}
    if (labels_node.get("pageInfo") or {}).get("hasNextPage"):
        raise Unknown(f"a bundle PR has more than {LABEL_PAGE} labels, so its label list is incomplete")
    labels = tuple(str(label["name"]) for label in (labels_node.get("nodes") or ()))
    return Member(str(node.get("body") or ""), labels)


def collect(payload: dict, repository: str, fetch: BodyFetcher) -> dict:
    """The record for this event; an unreadable bundle is an `unknown` record, never empty."""
    try:
        prs = bundle_prs(payload, repository)
        if prs is None:
            return {"bundle": False}
        return summarize(fetch(repository, prs), prs)
    except Unknown as unknown:
        return {"bundle": True, "unknown": str(unknown)}
    except Exception as error:  # a network or decode failure is UNKNOWN, loudly, below
        return {"bundle": True, "unknown": f"the bundle's PR bodies could not be fetched: {error!r}"}


# ----- CLI -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("collect", help="write this event's bundle record")
    run.add_argument("--event", required=True, help="the GitHub event payload (GITHUB_EVENT_PATH)")
    run.add_argument("--repository", required=True, help="owner/name (GITHUB_REPOSITORY)")
    run.add_argument("--out", required=True, help="where to write the one-line JSON record")
    arguments = parser.parse_args(argv)
    payload = json.loads(Path(arguments.event).read_text(encoding="utf-8"))
    token = os.environ.get("GH_TOKEN", "")

    def no_token(_repository: str, _prs: list[int]) -> dict[int, Member]:
        raise Unknown("GH_TOKEN is not set, so the bundle's PR bodies cannot be read")

    record = collect(payload, arguments.repository, graphql_bodies(token) if token else no_token)
    Path(arguments.out).write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
    if record.get("unknown"):
        print(f"::warning title=bundle declarations UNKNOWN::{record['unknown']}", file=sys.stderr)
    elif record["bundle"]:
        print(
            f"[bundle] {len(record['prs'])} PR(s), {len(record['declared'])} declared module(s), "
            f"{len(record['reasons'])} written reason(s)",
            file=sys.stderr,
        )
    else:
        print("[bundle] not a Trunk queue draft", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
