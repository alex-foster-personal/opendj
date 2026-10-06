#!/usr/bin/env python3
"""Can the Trunk merge-queue runner pool take work right now? (DEVOPS-18 hazard check)

ADR-NEW-trunk-queue-drafts-use-a-reserved-runner-pool routes every self-hosted job on a
Trunk queue draft's required path to `vars.CI_RUNS_ON_MERGE_QUEUE` when that variable is
set. GitHub never fails a job whose labels no runner carries: it queues it until one
appears (24 hours, then cancels). So a set variable with no ONLINE runner carrying every
one of its labels stalls every batch silently while each check just reads "queued". This
reads the live variable and the live runner list and says which case holds.

Read-only: two `gh api` listings (repository variables and self-hosted runners), both
needing a token that can read them (the fleet human token can). It never sets or deletes
a variable; `gh variable delete CI_RUNS_ON_MERGE_QUEUE` is the off switch it prints.

    python -m scripts.ci_merge_queue_pool                  # the live variable
    python -m scripts.ci_merge_queue_pool --assume '["self-hosted","linux","mq"]'
                                                           # preflight a value before setting it

Exit codes: 0 OK or OFF (variable unset), 1 STRANDED, 2 UNKNOWN (could not measure).
A WARN line (reserve shared with another CI_RUNS_ON_* pool) does not change the exit.

MINI-PRD
    R1 Stranded pool detection ....................................... done + ran + regression
       [if] the variable is set and zero ONLINE runners carry every label it names
            [then] print STRANDED and exit 1, never OK [else stop]
       [if] the variable is set and only OFFLINE runners match
            [then] still STRANDED, naming the offline matches [else stop]
       [if] the variable is unset
            [then] print OFF and exit 0: drafts use the general pools [else stop]
    R2 Honest measurement ............................................ done + ran + regression
       [if] a listing fails, is not JSON, or returns fewer items than its total_count
            [then] print UNKNOWN and exit 2, never a verdict [else stop]
       [if] the variable's value is not a JSON label or list of labels
            [then] UNKNOWN, exit 2 [else stop]
       [if] label case differs between the variable and a runner (`linux` vs `Linux`)
            [then] they still match, as GitHub's own matching does [else stop]
    R3 Reserve exclusivity ........................................... done + ran + regression
       [if] a matching runner is also matched by another CI_RUNS_ON_* variable
            [then] print WARN naming the runner and the variable, exit unchanged [else stop]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field

VARIABLE = "CI_RUNS_ON_MERGE_QUEUE"
VARIABLE_PREFIX = "CI_RUNS_ON_"
DEFAULT_REPO = "private_owner/music-dj-tools"
EXIT_OK, EXIT_STRANDED, EXIT_UNKNOWN = 0, 1, 2


class MeasurementError(RuntimeError):
    """A listing could not be read completely; the pool state is unmeasured."""


@dataclass(frozen=True)
class Runner:
    name: str
    online: bool
    busy: bool
    labels: frozenset[str]


@dataclass
class Verdict:
    status: str
    exit_code: int
    lines: list[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
def parse_labels(value: str) -> frozenset[str]:
    """The label set a `runs-on` JSON value asks for, lowercased as GitHub compares them."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise MeasurementError(f"{VARIABLE} is not JSON: {value!r} ({error})") from None
    labels = [parsed] if isinstance(parsed, str) else parsed
    if not isinstance(labels, list) or not labels or not all(isinstance(x, str) for x in labels):
        raise MeasurementError(f"{VARIABLE} is not a label or a list of labels: {value!r}")
    return frozenset(label.lower() for label in labels)


def _matching(wanted: frozenset[str], runners: list[Runner]) -> list[Runner]:
    return [runner for runner in runners if wanted <= runner.labels]


def pool_verdict(value: str | None, runners: list[Runner], other_pools: dict[str, str]) -> Verdict:
    """Classify the pool. `other_pools` maps every other CI_RUNS_ON_* variable to its value."""
    if value is None:
        return Verdict(
            "OFF",
            EXIT_OK,
            [f"[OFF] {VARIABLE} is unset: Trunk drafts use the general pools (route off)"],
        )
    wanted = parse_labels(value)
    matches = _matching(wanted, runners)
    online = sorted((r for r in matches if r.online), key=lambda r: r.name)
    offline = sorted(r.name for r in matches if not r.online)
    if not online:
        lines = [
            f"[STRANDED] {VARIABLE}={value} but 0 online runners carry every label "
            f"{sorted(wanted)}; every Trunk batch job will sit queued until one comes online",
            f"    offline matches: {', '.join(offline) or 'none'}",
            f"    off switch: gh variable delete {VARIABLE} --repo <owner>/<repo>",
        ]
        return Verdict("STRANDED", EXIT_STRANDED, lines)
    busy = sum(r.busy for r in online)
    lines = [
        f"[OK] {VARIABLE}={value}: {len(online)} online runner(s) match, {busy} busy: "
        + ", ".join(r.name for r in online)
    ]
    if offline:
        lines.append(f"    offline matches: {', '.join(offline)}")
    # Every match, online or not: an offline runner that also carries a general
    # pool's labels serves both queues the moment it comes back.
    for runner in sorted(matches, key=lambda r: r.name):
        shared = sorted(
            name
            for name, other in other_pools.items()
            if runner in _matching(parse_labels(other), [runner])
        )
        if shared:
            lines.append(
                f"[WARN] reserve not exclusive: {runner.name} also serves {', '.join(shared)}"
            )
    return Verdict("OK", EXIT_OK, lines)


# -----------------------------------------------------------------------------
def _gh_list(path: str, key: str) -> list[dict]:
    """Every item of a paginated `gh api` listing, refused if short of its total_count."""
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise MeasurementError(f"gh api {path} failed: {result.stderr.strip()[:300]}")
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise MeasurementError(f"gh api {path} returned non-JSON ({error})") from None
    return items_from_pages(pages, key, path)


def items_from_pages(pages: list[dict], key: str, path: str) -> list[dict]:
    """Flatten slurped pages, refusing a listing shorter than its own total_count."""
    if not pages:
        raise MeasurementError(f"gh api {path} returned no pages")
    items = [item for page in pages for item in page[key]]
    total = pages[0]["total_count"]
    if len(items) != total:
        raise MeasurementError(f"gh api {path}: read {len(items)} of total_count {total}")
    return items


def _runner(item: dict) -> Runner:
    return Runner(
        name=item["name"],
        online=item["status"] == "online",
        busy=bool(item["busy"]),
        labels=frozenset(label["name"].lower() for label in item["labels"]),
    )


def measure(repo: str, assume: str | None) -> Verdict:
    variables = {
        v["name"]: v["value"]
        for v in _gh_list(f"repos/{repo}/actions/variables?per_page=30", "variables")
        if v["name"].startswith(VARIABLE_PREFIX)
    }
    runners = [
        _runner(item) for item in _gh_list(f"repos/{repo}/actions/runners?per_page=100", "runners")
    ]
    value = assume if assume is not None else variables.get(VARIABLE)
    others = {name: v for name, v in variables.items() if name != VARIABLE}
    return pool_verdict(value, runners, others)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name to read")
    parser.add_argument(
        "--assume",
        metavar="JSON",
        help=f"evaluate this {VARIABLE} value against the live runners instead of the live one",
    )
    args = parser.parse_args(argv)
    try:
        verdict = measure(args.repo, args.assume)
    except MeasurementError as error:
        print(f"[UNKNOWN] {error}")
        return EXIT_UNKNOWN
    print("\n".join(verdict.lines))
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
