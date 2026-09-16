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
    BASELINE_MISMATCH  a failed job with no identity whose NAME also failed on main, where
                   main's failure DID name failing tests. The two failures share a name and
                   nothing else, so the comparison was never made. Unmeasured.
    RATCHET_DEBT   quality ratchet breach, which ships and is debt-logged under SHIP mode.
    UNEXPLAINED    identities were found, all known, and the job ALSO failed for a reason no
                   test accounts for (cancel, timeout, kill, a non-1 exit). Unmeasured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# The tests the fleet tolerates as flaky, mirroring KNOWN_FLAKES in nucbox
# ~/jobs/sweep/gate-fast.sh. Stored as LITERALS, not as a regular expression: as a pattern
# they went into the identity unescaped (`.` matched any character) and unanchored, so any
# identity merely CONTAINING one was excused. Sol's P1 on #3293 -- a new test named
# `..._thread_teardown`, or a spec under `my_setup-entry-points.spec.ts`, could fail
# genuinely and still take the mergeable KNOWN_RED verdict off a name it only extends.
KNOWN_FLAKE_IDENTITIES = (
    "setup-entry-points.spec.ts",
    "test_lifespan_runs_and_joins_a_real_lyric_index_thread",
    "prefs-golden-blob.test.mjs",
)
# Identities are `FAILED tests/a/b.py::name`, `[chromium] (U+203A) tests/s.ts:4:1`,
# `not ok - name` and `svelte-check path:line`, so a flake name is bounded by a path or
# node-id separator on the left and ends the identity or is followed by one on the right.
# A parametrized variant (`name[case]`) does NOT match, which is the safe direction: it
# reads GENUINE and a person looks, rather than inheriting another test's excuse.
KNOWN_FLAKES = re.compile(
    r"(?:^|[/:\s])(?:"
    + "|".join(re.escape(name) for name in KNOWN_FLAKE_IDENTITIES)
    + r")(?=$|[:\s])"
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
    BASELINE_MISMATCH = "BASELINE_MISMATCH"
    RATCHET_DEBT = "RATCHET_DEBT"
    BASELINE_UNREADABLE = "BASELINE_UNREADABLE"
    UNEXPLAINED = "UNEXPLAINED"


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


# A runner line that a failing test never produces. Exit code 1 is what pytest, vitest and
# playwright return when tests fail, so it is the ONE code that test identities can explain;
# 137 (killed), 143 (terminated), 124 (timeout) and 2 (interrupted, usage error) cannot be.
_BEYOND_TESTS = re.compile(
    r"The operation was canceled"
    r"|exceeded the maximum execution time"
    r"|No space left on device"
    r"|Process completed with exit code (?!1\b)[0-9]+"
)


_STEP_ECHO = "##[group]Run "
_EXIT_ONE = "Process completed with exit code 1"
# The quality ratchet's own words for a breach (`scripts/quality_gate.py`). A breach is an
# identity-free exit 1 BY DESIGN -- no test failed, a metric got worse -- and it is the one
# such exit the fleet ships as debt. Sol's P2 on #3293: without this the ratchet's own step
# was read as an exit nothing accounted for, every ordinary ratchet log came back
# UNEXPLAINED, and the documented mergeable debt path ended UNKNOWN on every real run.
_OWNED_EXIT = re.compile(r"\[quality\] (?:FAIL: [0-9]+ metric\(s\) got worse|REGRESSION\b)")


def unowned_exit_steps(log: str) -> list[str]:
    """Steps that ended with exit code 1 while naming no failing test.

    Exit code 1 is the ONE code a test failure can explain, so `_BEYOND_TESTS` deliberately
    leaves it alone -- and that is what lets a job which runs tests in one step and a build,
    typecheck or ratchet in another carry known-red identities from the first while the
    SECOND is what failed. The runner opens every step with `##[group]Run <command>`, so the
    step that owns a terminating error is the one whose slice holds it. A step OWNS its own
    exit when it names a failing test in that slice, or when it states its own breach there:
    the quality ratchet fails on a worsened metric with no test involved, and saying so is
    an explanation, not a silence.

    A log with no step echoes cannot be partitioned, so it returns empty: that is UNKNOWN,
    not proof, exactly as `failure_beyond_tests` documents for its own empty result.
    """
    lines = [_strip_prefixes(raw) for raw in log.split("\n")]
    starts = [i for i, line in enumerate(lines) if _STEP_ECHO in line]
    if not starts:
        return []
    found: list[str] = []
    for start, end in zip(starts, [*starts[1:], len(lines)], strict=True):
        step = lines[start:end]
        if not any(_EXIT_ONE in _ANSI.sub("", line) for line in step):
            continue
        body = "\n".join(step)
        if failed_identities(body) or _OWNED_EXIT.search(_ANSI.sub("", body)):
            continue
        command = _ANSI.sub("", lines[start].split(_STEP_ECHO, 1)[1]).strip()
        found.append(f"exit code 1 in a step that named no failing test: {command}"[:200])
        if len(found) == EXCERPT_LINES:
            break
    return found


def unpartitionable_exit(log: str) -> bool:
    """True when the log ends in exit code 1 and nothing says WHICH step spent it.

    `unowned_exit_steps` returns an empty list for two OPPOSITE reasons: no step spent an
    exit no test accounts for, and the log could not be partitioned into steps at all. Its
    own docstring calls the second one UNKNOWN, and then every caller read empty as "the
    tests own this exit" and got a mergeable KNOWN_RED out of a measurement that never
    happened. Sol's P1 on #3293. Asked separately, the caller can tell the two apart.
    """
    lines = [_strip_prefixes(raw) for raw in log.split("\n")]
    if not any(_EXIT_ONE in _ANSI.sub("", line) for line in lines):
        return False
    return not any(_STEP_ECHO in line for line in lines)


def failure_beyond_tests(log: str) -> list[str]:
    """Runner lines saying the job failed for something no test failure accounts for.

    Returns the evidence rather than a bare bool so the watch can print WHY a job it would
    otherwise have called known-red is unmeasured instead. Empty means no such evidence was
    found, which is not the same as proof there is none: this reads only the runner's own
    terminal lines, and a silent truncation leaves nothing to match.
    """
    found: list[str] = []
    for raw in log.split("\n"):
        line = _ANSI.sub("", _strip_prefixes(raw)).strip()
        if _BEYOND_TESTS.search(line):
            found.append(line[:200])
            if len(found) == EXCERPT_LINES:
                break
    if len(found) < EXCERPT_LINES:
        found.extend(unowned_exit_steps(log)[: EXCERPT_LINES - len(found)])
    return found


def classify_job(
    job_name: str,
    identities: frozenset[str],
    main_red: frozenset[str],
    main_red_job_names: frozenset[str],
    baseline_unreadable_job_names: frozenset[str] = frozenset(),
    *,
    beyond_tests: bool,
) -> JobClassification:
    """One failed job's verdict, in the order the nucbox merge gate applies its rules.

    A ratchet job carries debt only when nothing NEW failed inside it. Keyed on the job name
    alone, a genuine regression landing in a ratchet job reads RATCHET_DEBT, the watcher ends
    KNOWN_RED_ONLY, and the agent merges past a real failure.
    """
    residual = frozenset(i for i in identities if not KNOWN_FLAKES.search(i)) - main_red

    def verdict(kind: JobVerdict) -> JobClassification:
        return JobClassification(kind, identities, residual)

    if identities and not residual:
        if beyond_tests:
            # The identities are real but they are not the whole failure. Calling this
            # KNOWN_RED lets a job that ALSO hit a cap kill, a timeout or a build error merge
            # on the strength of tests that were already red, the other failure never read.
            return verdict(JobVerdict.UNEXPLAINED)
        return verdict(JobVerdict.KNOWN_RED)
    if not identities and INFRA_CLASS_JOBS.search(job_name):
        return verdict(JobVerdict.INFRA)
    if RATCHET_JOBS.search(job_name) and not residual:
        if beyond_tests:
            # Sol's P1 on #3293. A ratchet breach SHIPS as debt, which makes this the only
            # mergeable zero-identity verdict, and the branch was granting it without
            # reading the evidence that the job ALSO timed out, was killed or ran out of
            # disk. A ratchet that did not finish measured no ratchet.
            return verdict(JobVerdict.UNEXPLAINED)
        return verdict(JobVerdict.RATCHET_DEBT)
    if not identities and job_name in main_red_job_names:
        # Sol's P1 on #3293. A name is not a cause. Every name in `main_red_job_names` is one
        # whose main log was READ and named failing TESTS, and this job named none, so main's
        # failure and this one are established to be DIFFERENT. Trusting the shared name
        # merges a build, typecheck or budget regression on the strength of main's unrelated
        # test failures.
        return verdict(JobVerdict.BASELINE_MISMATCH)
    if not identities and job_name in baseline_unreadable_job_names:
        # Main failed this job too, but its log could not be read, so nothing establishes
        # that the two failures are the same. Unmeasured, never mergeable.
        return verdict(JobVerdict.BASELINE_UNREADABLE)
    return verdict(JobVerdict.GENUINE)
