"""Failing test identities in a GitHub Actions job log, and what they mean for a merge.

Pure. The fleet merge gate on nucbox (`~/jobs/sweep/failed-ids.sh`, `gate-fast.sh`) and
`scripts/ci_watch.py` must read a log the same way, or the watcher calls GENUINE what the
gate calls main's red. This module is a line-for-line port of that parser and of the gate's
per-job classifier, pinned by `tests/scripts/test_ci_failure_ids.py`.

Identities, one per failure:
    pytest        FAILED|ERROR tests/<nodeid>
    node test     not ok - <name>, from TAP `not ok N - <name>` or the spec reporter's
                  `✖ <name> (<N>ms)`. The TAP number is dropped: it shifts when a test is added.
    Playwright    [<project>] (U+203A) tests/<spec>:<line>:<col>, read ONLY from the "N failed"
                  summary block; a progress line is a pass and the "N flaky" block passed on retry.
    svelte-check  svelte-check <repo path>:<line>, one per Error block, never a Warn block.

Job verdicts (`classify_job`):
    GENUINE        a failure not on main and not a known flake, or a failed job with zero
                   identities that is neither pytest infra nor red on main: loud, never passed.
    KNOWN_RED      every identity is on main or a known flake.
    INFRA          a pytest or canary job with no identity at all (cap kill, runner reclaim).
    MAIN_RED_JOB   a failed job with no identity whose NAME is also red on main's head.
    RATCHET_DEBT   quality ratchet breach, which ships and is debt-logged under SHIP mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# Mirrors KNOWN_FLAKES in nucbox ~/jobs/sweep/gate-fast.sh, matched as a substring search.
KNOWN_FLAKES = re.compile(
    r"setup-entry-points.spec.ts|test_lifespan_runs_and_joins_a_real_lyric_index_thread"
    r"|prefs-golden-blob.test.mjs"
)
INFRA_CLASS_JOBS = re.compile(r"pytest fast lane|affected-test canary")
RATCHET_JOBS = re.compile(r"quality ratchet")

_RUNNER_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PYTEST = re.compile(r"(?:FAILED|ERROR) tests/\S+")
_TAP = re.compile(r"not ok [0-9]+ - .*\S")
_SPEC = re.compile(r"^ *✖ (.*[^ ]) \([0-9.]+m?s\)$")
_PLAYWRIGHT_HEADER = re.compile(r"^ +[0-9]+ failed *$")
_PLAYWRIGHT_ENTRY = re.compile("^ +\\[[^\\]]+\\] \u203a tests/[^ ]+:[0-9]+:[0-9]+")
_SVELTE_LOCATION = re.compile(r"^/.*:[0-9]+:[0-9]+$")
_SVELTE_CHECKOUT = re.compile(r"^.*/_work/[^/]+/[^/]+/")


class JobVerdict(StrEnum):
    GENUINE = "GENUINE"
    KNOWN_RED = "KNOWN_RED"
    INFRA = "INFRA"
    MAIN_RED_JOB = "MAIN_RED_JOB"
    RATCHET_DEBT = "RATCHET_DEBT"


@dataclass(frozen=True)
class JobClassification:
    verdict: JobVerdict
    identities: frozenset[str]
    residual: frozenset[str]


# ----- parsing -----


def _strip_prefixes(line: str) -> str:
    return _TIMESTAMP.sub("", _RUNNER_PREFIX.sub("", line, count=1), count=1)


def _playwright_identities(lines: list[str]) -> set[str]:
    found: set[str] = set()
    in_block = False
    for line in lines:
        if _PLAYWRIGHT_HEADER.search(line):
            in_block = True
        elif in_block and (entry := _PLAYWRIGHT_ENTRY.search(line)):
            found.add(entry.group(0).lstrip(" "))
        else:
            in_block = False
    return found


def _svelte_check_identities(lines: list[str]) -> set[str]:
    found: set[str] = set()
    location = ""
    for raw in lines:
        line = _ANSI.sub("", raw)
        if _SVELTE_LOCATION.search(line):
            location = line
            continue
        if location and line.startswith("Error: "):
            path = re.sub(r":[0-9]+$", "", _SVELTE_CHECKOUT.sub("", location, count=1), count=1)
            found.add(f"svelte-check {path}")
        location = ""
    return found


def failed_identities(log: str) -> frozenset[str]:
    """Every failing test identity in one job log."""
    lines = [_strip_prefixes(line) for line in log.split("\n")]
    found: set[str] = set()
    for line in lines:
        found.update(_PYTEST.findall(line))
        if tap := _TAP.search(line):
            found.add(re.sub(r"^not ok [0-9]+ - ", "not ok - ", tap.group(0), count=1))
        if spec := _SPEC.search(line):
            found.add(f"not ok - {spec.group(1)}")
    found |= _playwright_identities(lines)
    found |= _svelte_check_identities(lines)
    return frozenset(found)


_ERROR_LINE = re.compile(r"\[ERROR\]|##\[error\]|^Error: |^error: ", re.IGNORECASE)
_COMMAND_ECHO = "\x1b[36;1m"
EXCERPT_LINES = 3


def error_excerpt(log: str) -> list[str]:
    """The first error lines of a log with no test identity, so a zero-identity failure says
    what failed, as the merge gate's excerpt does. The runner's echo of the step script is
    skipped: it quotes commands, not results."""
    found: list[str] = []
    for raw in log.split("\n"):
        if _COMMAND_ECHO in raw:
            continue
        line = _ANSI.sub("", _strip_prefixes(raw)).strip()
        if _ERROR_LINE.search(line) and "Process completed with exit code" not in line:
            found.append(line[:200])
            if len(found) == EXCERPT_LINES:
                break
    return found


def classify_job(
    job_name: str,
    identities: frozenset[str],
    main_red: frozenset[str],
    main_red_job_names: frozenset[str],
) -> JobClassification:
    """One failed job's verdict, in the order the nucbox merge gate applies its rules."""
    residual = frozenset(i for i in identities if not KNOWN_FLAKES.search(i)) - main_red

    def verdict(kind: JobVerdict) -> JobClassification:
        return JobClassification(kind, identities, residual)

    if identities and not residual:
        return verdict(JobVerdict.KNOWN_RED)
    if not identities and INFRA_CLASS_JOBS.search(job_name):
        return verdict(JobVerdict.INFRA)
    if RATCHET_JOBS.search(job_name):
        return verdict(JobVerdict.RATCHET_DEBT)
    if not identities and job_name in main_red_job_names:
        return verdict(JobVerdict.MAIN_RED_JOB)
    return verdict(JobVerdict.GENUINE)
