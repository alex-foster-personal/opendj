"""Turn a pull request's diff into a pytest plan: SKIP_PYTEST, SCOPED or FULL.

SMARTEST-CI round 5 (`specs/ci-fail-fast.md`). This REPORTS a plan; nothing acts on it.
Selection becomes a gate only once the miss audit has produced a recall number, which it has
not, so the only safe way to be wrong here is to over-select, and every rule below that
cannot decide answers FULL.

Scope names are the module names Mergify's `mergify ci scopes` will use, so queue batching
by scope needs no renames later.

What the verdicts mean:

    SKIP_PYTEST  no changed path touches any scope, any always-run test, or any FULL
                 trigger. Documentation and workflow-free config changes land here.
    SCOPED       the union of every touched scope's tests, plus the always-run tests that
                 no scope claims.
    FULL         a FULL trigger, a deleted or renamed `.py`, a changed source path no scope
                 claims, or a selection so large that narrowing buys nothing.

An EMPTY change list is none of these. A diff that read nothing and a diff of nothing look
identical from here, and `scripts/affected_tests.py` shipped that exact hole (0 modules,
exit 0, a canary green 25 runs of 28 without running a test). `plan` raises instead.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO / "ci" / "test-scopes.yml"
CAP_FRACTION = 0.6


class PlanError(Exception):
    """The plan could not be MEASURED. Never rendered as a verdict."""


class Verdict(StrEnum):
    SKIP_PYTEST = "SKIP_PYTEST"
    SCOPED = "SCOPED"
    FULL = "FULL"


@dataclass(frozen=True)
class Change:
    """One line of `git diff --name-status`: the status letter and the path."""

    status: str
    path: str


@dataclass(frozen=True)
class Scope:
    name: str
    sources: tuple[str, ...]
    tests: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    full_triggers: tuple[str, ...]
    always: tuple[str, ...]
    scopes: tuple[Scope, ...]


@dataclass(frozen=True)
class Plan:
    verdict: Verdict
    scopes: tuple[str, ...]
    test_paths: tuple[str, ...]
    reason: str


# ----- pure: pattern matching -----


def _regex(pattern: str) -> re.Pattern[str]:
    """A glob as a regex. `**` crosses directories, `*` does not, a trailing `/` is a prefix.

    Written out rather than delegated to `fnmatch`, whose `*` crosses `/` and would make
    `apps/*/cli.py` match `apps/a/b/cli.py`.
    """
    if pattern.endswith("/"):
        return re.compile(re.escape(pattern) + ".*")
    table = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
    parts = re.split(r"(\*\*/|\*\*|\*|\?)", pattern)
    return re.compile("".join(table.get(part, re.escape(part)) for part in parts) + "$")


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_regex(pattern).match(path) for pattern in patterns)


# ----- pure: the plan -----

_SOURCE_SUFFIXES = (".py", ".sh", ".ts", ".svelte", ".sql", ".toml")
_SOURCE_ROOTS = ("apps/", "scripts/", "ops/", "tests/")


def _is_source(path: str) -> bool:
    return path.startswith(_SOURCE_ROOTS) and path.endswith(_SOURCE_SUFFIXES)


def plan(
    changes: tuple[Change, ...], config: Config, cap_fraction: float = CAP_FRACTION
) -> Plan:
    """The plan for one diff. Pure: no filesystem, no git, no network."""
    if not changes:
        raise PlanError(
            "the change list is empty, which a diff of nothing and a diff that failed to "
            "read both produce; refusing to report a verdict"
        )
    if not config.scopes:
        raise PlanError("the scope config declares no scopes")

    removed = [c.path for c in changes if c.status[:1] in ("D", "R") and c.path.endswith(".py")]
    if removed:
        return Plan(
            Verdict.FULL,
            (),
            (),
            f"a .py file was deleted or renamed: {removed[0]}",
        )

    paths = [c.path for c in changes]
    triggered = [p for p in paths if matches(p, config.full_triggers)]
    if triggered:
        return Plan(Verdict.FULL, (), (), f"full trigger: {triggered[0]}")

    hit: set[str] = set()
    unclaimed_source: list[str] = []
    for path in paths:
        owners = [s.name for s in config.scopes if matches(path, s.sources + s.tests)]
        if owners:
            hit.update(owners)
        elif matches(path, config.always):
            hit.add(_ALWAYS)
        elif _is_source(path):
            unclaimed_source.append(path)
    if unclaimed_source:
        return Plan(
            Verdict.FULL,
            (),
            (),
            f"a source path no scope claims: {unclaimed_source[0]}",
        )
    if not hit:
        return Plan(Verdict.SKIP_PYTEST, (), (), "no changed path touches a scope")

    named = tuple(sorted(hit - {_ALWAYS}))
    if len(named) > max(1, int(len(config.scopes) * cap_fraction)):
        return Plan(
            Verdict.FULL,
            named,
            (),
            f"{len(named)} of {len(config.scopes)} scopes selected, over the cap",
        )
    tests = {t for s in config.scopes if s.name in named for t in s.tests}
    return Plan(
        Verdict.SCOPED,
        named,
        tuple(sorted(tests | set(config.always))),
        f"{len(named)} scope(s) touched",
    )


_ALWAYS = "<always>"


# ----- I/O: config and git -----


def read_config(path: Path = CONFIG_PATH) -> Config:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanError(f"cannot read {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise PlanError(f"{path} is not a mapping: {raw!r}")
    scopes = raw.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise PlanError(f"{path} declares no scopes")
    return Config(
        tuple(raw.get("full_triggers") or ()),
        tuple(raw.get("always") or ()),
        tuple(
            Scope(entry["name"], tuple(entry["sources"]), tuple(entry["tests"]))
            for entry in scopes
        ),
    )


def read_changes(base: str, head: str) -> tuple[Change, ...]:
    """`git diff --name-status`, refusing anything that did not run cleanly."""
    result = subprocess.run(
        ["git", "diff", "--name-status", "-M", f"{base}...{head}"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    if result.returncode != 0:
        raise PlanError(f"git diff {base}...{head} failed: {result.stderr.strip()}")
    changes: list[Change] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        # A rename reports OLD and NEW; both are changed paths, and the old one is a delete.
        changes.extend(Change(fields[0], path) for path in fields[1:])
    return tuple(changes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)
    try:
        result = plan(read_changes(args.base, args.head), read_config(args.config))
    except PlanError as exc:
        print(f"UNKNOWN: {exc}", file=sys.stderr)
        return 3
    print(f"verdict={result.verdict}")
    print(f"reason={result.reason}")
    print(f"scopes={','.join(result.scopes)}")
    print(f"tests={' '.join(result.test_paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
