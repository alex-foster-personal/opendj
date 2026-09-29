"""Trunk CI (main pushes, ci:trunk-repair PRs) resolves to agentbox pytest runners only.

ADR-NEW-trunk-ci-runs-on-agentbox-hosts-only (Sat 26 Sep 2026, the ADR-0041
priority-pool lever). By then the `pytest` label sat on 9 agentbox runners and
14 nucbox-wsl runners, and main's checkpoint at cf36185ad (run 36237434128)
put 3 of 5 shards on nucbox: 31-40 min there, one at the wall budget with 0
failed, against 20 min on agentbox. CI_RUNS_ON_TRUNK names a label
(`trunk-agentbox`) that only the agentbox pytest runners carry, and a guarded
disjunct prefers it for the trunk job classes.

The existing pins in test_ci_main_fix_runner_reserve.py, test_ci_shard_matrix.py
and test_ci_fast_tier_job.py compare expression STRINGS. This file EVALUATES the
expressions with a small evaluator for the GitHub Actions expression subset they
use (`==`, `&&`, `||`, parentheses, `contains`, `startsWith`, `fromJSON`, `vars.*`
and `github.*` paths), so the routing claims below are checked as behavior, not as
text. Its own semantics are pinned by a control case against the pre-change
expression, which must resolve to today's pools.
tests/scripts/test_ci_merge_queue_runner_pool.py reuses it for the Trunk draft
route (ADR-NEW-trunk-queue-drafts-use-a-reserved-runner-pool).

Regression lines:
  - if a main push does not resolve to CI_RUNS_ON_TRUNK when it is set then main's
    shards can land on nucbox again
  - if a trunk-repair PR's shards leave the main-fix reserve while it is set then the
    repair loses its dedicated pair
  - if a trunk-repair PR's fast legs do not resolve to CI_RUNS_ON_TRUNK then its green
    head depends on a nucbox leg
  - if an ordinary PR or a non-main push resolves to CI_RUNS_ON_TRUNK then PR load is
    routed by a trunk-only lever
  - if an unset CI_RUNS_ON_TRUNK does not resolve exactly as the pre-change expression
    did then rollback is not a variable unset
  - if the trunk-tip sweeper reads CI_RUNS_ON_TRUNK then it can queue behind the main
    shards it cancels
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TRUNK_TIP_ONLY = REPO_ROOT / ".github" / "workflows" / "trunk-tip-only.yml"

MAIN_FIX = '["self-hosted","linux","main-fix"]'
TRUNK = '["self-hosted","linux","trunk-agentbox"]'
PYTEST = '["self-hosted","linux","pytest"]'
FAST = '["self-hosted","linux","pytest"]'
E2E = '["self-hosted","linux","e2e"]'
LINUX = '["self-hosted","linux","agentbox"]'
REPOSITORY = "owner/music-dj-tools"
LIVE_VARS = {
    "CI_RUNS_ON_MAIN_FIX": MAIN_FIX,
    "CI_RUNS_ON_TRUNK": TRUNK,
    "CI_RUNS_ON_PYTEST": PYTEST,
    "CI_RUNS_ON_FAST": FAST,
    "CI_RUNS_ON_E2E": E2E,
    "CI_RUNS_ON_LINUX": LINUX,
}

#: The shard job's runs-on on main fbd34ca68, before this change: the control.
PRE_CHANGE_SHARD = (
    "${{ fromJSON((github.event_name == 'pull_request' && "
    "contains(github.event.pull_request.labels.*.name, 'ci:trunk-repair')) && "
    "vars.CI_RUNS_ON_MAIN_FIX || vars.CI_RUNS_ON_PYTEST || vars.CI_RUNS_ON_E2E || "
    "vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
)
PRE_CHANGE_FAST = (
    "${{ fromJSON(vars.CI_RUNS_ON_FAST || vars.CI_RUNS_ON_PYTEST || vars.CI_RUNS_ON_E2E || "
    "vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
)


# ----------------------------------------------------------------- evaluator


@dataclass(frozen=True)
class Event:
    name: str
    ref: str
    labels: tuple[str, ...] = ()
    variables: dict[str, str] = field(default_factory=dict)
    head_ref: str = ""
    author: str = "a-human"
    head_repo: str = REPOSITORY
    repository: str = REPOSITORY

    def context(self) -> dict[str, Any]:
        event: dict[str, Any] = {}
        github: dict[str, Any] = {
            "event_name": self.name,
            "ref": self.ref,
            "repository": self.repository,
            "event": event,
        }
        if self.name == "pull_request":
            event["pull_request"] = {
                "labels": [{"name": n} for n in self.labels],
                "user": {"login": self.author},
                "head": {"repo": {"full_name": self.head_repo}},
            }
            github["head_ref"] = self.head_ref
        return {"github": github, "vars": self.variables}


_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>==|&&|\|\||[(),])|(?P<path>[A-Za-z_][\w.*-]*))"
)


def _tokens(expr: str) -> list[str]:
    out, pos = [], 0
    while pos < len(expr.rstrip()):
        match = _TOKEN.match(expr, pos)
        assert match and match.lastgroup, f"evaluator cannot tokenize at {expr[pos : pos + 30]!r}"
        out.append(match.group(match.lastgroup))
        pos = match.end()
    return out


def _truthy(value: Any) -> bool:
    return value not in (None, False, 0, "")


def _resolve_path(path: str, ctx: dict[str, Any]) -> Any:
    value: Any = ctx
    for part in path.split("."):
        if part == "*":
            return value  # the only `*` use here is labels.*.name, handled below
        if isinstance(value, list):
            value = [item.get(part) for item in value if isinstance(item, dict)]
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


class _Parser:
    def __init__(self, tokens: list[str], ctx: dict[str, Any]) -> None:
        self.tokens, self.ctx, self.i = tokens, ctx, 0

    def _peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _take(self, expected: str | None = None) -> str:
        token = self.tokens[self.i]
        assert expected is None or token == expected, f"expected {expected!r}, got {token!r}"
        self.i += 1
        return token

    def parse(self) -> Any:
        value = self._or()
        assert self._peek() is None, f"trailing tokens: {self.tokens[self.i :]}"
        return value

    def _or(self) -> Any:
        value = self._and()
        while self._peek() == "||":
            self._take()
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self) -> Any:
        value = self._eq()
        while self._peek() == "&&":
            self._take()
            right = self._eq()
            value = right if _truthy(value) else value
        return value

    def _eq(self) -> Any:
        value = self._primary()
        while self._peek() == "==":
            self._take()
            right = self._primary()
            value = str(value).lower() == str(right).lower()
        return value

    def _primary(self) -> Any:
        token = self._take()
        if token == "(":
            value = self._or()
            self._take(")")
            return value
        if token.startswith("'"):
            return token[1:-1].replace("''", "'")
        if self._peek() == "(":
            self._take("(")
            args = [self._or()]
            while self._peek() == ",":
                self._take()
                args.append(self._or())
            self._take(")")
            if token == "contains":
                haystack, needle = args
                return isinstance(haystack, list) and any(
                    str(item).lower() == str(needle).lower() for item in haystack
                )
            if token == "startsWith":
                haystack, needle = args
                return str(haystack or "").lower().startswith(str(needle).lower())
            if token == "fromJSON":
                return json.loads(args[0])
            raise AssertionError(f"evaluator does not know function {token!r}")
        if token.endswith(".*.name"):
            items = _resolve_path(token[: -len(".*.name")], self.ctx)
            return [item.get("name") for item in items] if isinstance(items, list) else None
        value = _resolve_path(token, self.ctx)
        return "" if value is None and token.startswith("vars.") else value


def resolve(runs_on: str, event: Event) -> Any:
    inner = runs_on.strip()
    assert inner.startswith("${{") and inner.endswith("}}"), runs_on
    return _Parser(_tokens(inner[3:-2]), event.context()).parse()


def _runs_on(job_id: str) -> str:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"][job_id]["runs-on"]


def _without(name: str) -> dict[str, str]:
    return {k: v for k, v in LIVE_VARS.items() if k != name}


MAIN_PUSH = Event("push", "refs/heads/main", variables=LIVE_VARS)
REPAIR_PR = Event("pull_request", "refs/pull/1/merge", ("ci:trunk-repair",), LIVE_VARS)
PLAIN_PR = Event("pull_request", "refs/pull/2/merge", ("area:ci",), LIVE_VARS)
BRANCH_PUSH = Event("push", "refs/heads/af--feature", variables=LIVE_VARS)
MAIN_DISPATCH = Event("workflow_dispatch", "refs/heads/main", variables=LIVE_VARS)


# --------------------------------------------------------------------- tests


def test_evaluator_control_reproduces_todays_routing() -> None:
    """if the evaluator misreads the pre-change expression then no routing claim is measured"""
    assert resolve(PRE_CHANGE_SHARD, MAIN_PUSH) == json.loads(PYTEST)
    assert resolve(PRE_CHANGE_SHARD, REPAIR_PR) == json.loads(MAIN_FIX)
    assert resolve(PRE_CHANGE_SHARD, PLAIN_PR) == json.loads(PYTEST)
    assert resolve(PRE_CHANGE_FAST, REPAIR_PR) == json.loads(FAST)
    empty = Event("push", "refs/heads/main", variables={})
    assert resolve(PRE_CHANGE_SHARD, empty) == "ubuntu-latest"


def test_main_push_shards_resolve_to_the_trunk_pool() -> None:
    """if a main push does not resolve to CI_RUNS_ON_TRUNK then its shards can land on nucbox"""
    assert resolve(_runs_on("test"), MAIN_PUSH) == json.loads(TRUNK)


def test_trunk_repair_shards_keep_the_reserve_then_fall_to_the_trunk_pool() -> None:
    """if a trunk-repair PR's shards leave a set main-fix reserve then it loses its pair"""
    assert resolve(_runs_on("test"), REPAIR_PR) == json.loads(MAIN_FIX)
    no_reserve = Event(
        REPAIR_PR.name, REPAIR_PR.ref, REPAIR_PR.labels, _without("CI_RUNS_ON_MAIN_FIX")
    )
    assert resolve(_runs_on("test"), no_reserve) == json.loads(TRUNK)


def test_trunk_repair_fast_legs_resolve_to_the_trunk_pool() -> None:
    """if trunk-repair fast legs do not resolve to CI_RUNS_ON_TRUNK then a leg can be nucbox"""
    assert resolve(_runs_on("fast"), REPAIR_PR) == json.loads(TRUNK)


@pytest.mark.parametrize(
    "event",
    [PLAIN_PR, BRANCH_PUSH, MAIN_DISPATCH],
    ids=["plain-pr", "branch-push", "main-dispatch"],
)
def test_other_events_never_take_the_trunk_pool(event: Event) -> None:
    """if an ordinary PR or non-main push takes CI_RUNS_ON_TRUNK then a trunk lever routes PRs"""
    assert resolve(_runs_on("test"), event) == resolve(PRE_CHANGE_SHARD, event)
    assert resolve(_runs_on("fast"), event) == resolve(PRE_CHANGE_FAST, event)


@pytest.mark.parametrize(
    "event",
    [MAIN_PUSH, REPAIR_PR, PLAIN_PR, BRANCH_PUSH, MAIN_DISPATCH],
    ids=["main-push", "repair-pr", "plain-pr", "branch-push", "main-dispatch"],
)
def test_unset_trunk_variable_is_exactly_todays_routing(event: Event) -> None:
    """if an unset CI_RUNS_ON_TRUNK differs from the pre-change routing then rollback breaks"""
    unset = Event(event.name, event.ref, event.labels, _without("CI_RUNS_ON_TRUNK"))
    assert resolve(_runs_on("test"), unset) == resolve(PRE_CHANGE_SHARD, unset)
    assert resolve(_runs_on("fast"), unset) == resolve(PRE_CHANGE_FAST, unset)
    bare = Event(event.name, event.ref, event.labels, {})
    assert resolve(_runs_on("test"), bare) == "ubuntu-latest"
    assert resolve(_runs_on("fast"), bare) == "ubuntu-latest"


def test_trunk_tip_sweeper_stays_off_the_trunk_pool() -> None:
    """if the trunk-tip sweeper reads CI_RUNS_ON_TRUNK then it queues behind main's shards"""
    text = TRUNK_TIP_ONLY.read_text(encoding="utf-8")
    assert "vars.CI_RUNS_ON_MAIN_FIX" in text, (
        "positive control: the sweeper's own variable must be found"
    )
    assert "CI_RUNS_ON_TRUNK" not in text
