"""Checkers for `.github/workflows/runner-canary.yml`, each returning the problems it finds,
plus a minimal GitHub expression evaluator. tests/scripts/test_runner_canary_workflow.py
runs them against the real files and against mutations of them, so every checker is shown
able to go red."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from tests.scripts.ci_runner_routes import CANARY_RUNS_ON, CANARY_VENDOR_MATRIX

REPO = Path(__file__).resolve().parents[2]


CANARY_PATH = REPO / ".github" / "workflows" / "runner-canary.yml"


CI_PATH = REPO / ".github" / "workflows" / "ci.yml"


CONFIG = json.loads((REPO / "ci" / "runner-canary.json").read_text())


GATE_JOB = "budget-gate"


SHARD_JOB = "pytest"


EXPECTED_RUNS_ON = CANARY_RUNS_ON


EXPECTED_VENDOR_MATRIX = CANARY_VENDOR_MATRIX


WORKFLOW_DIR = CANARY_PATH.parent


CANARY_REF = "refs/heads/canary/" + "0" * 40


#: ci.yml `test` steps the canary deliberately omits, all trailing. The first three are
#: non-verdict uploads: their artifact names would collide across vendors in one run, and
#: their readers (the shard rebalance and the Mergify CI Insights job) live in the source
#: repository. The fourth is the Trunk-quarantine verdict, which reads a list job the
#: canary does not have (ADR-NEW-trunk-flaky-quarantine-on); the canary instead keeps its
#: pytest step failing on its own, see CI_ONLY_PYTEST_KEYS.
CI_ONLY_STEPS = (
    "Upload this shard's measured durations",
    "Stage this shard's JUnit report for the isolated CI Insights job",
    "Upload this shard's JUnit report for CI Insights",
    "Fast lane verdict (pytest exit code, Trunk quarantine applied)",
)


#: Keys ci.yml's pytest step carries that the canary's must NOT: continue-on-error defers
#: ci.yml's verdict to the step above, and the canary has no such step, so there it would
#: turn every red shard green.
CI_ONLY_PYTEST_KEYS = ("continue-on-error",)


#: The one in-step difference: the shard's own wall budget, sized under the 30-minute cap.
PYTEST_BUDGET_SUBSTITUTION = ("MDT_PYTEST_TIMEOUT_S=2400\n", "MDT_PYTEST_TIMEOUT_S=1440\n")


#: Round 1 (Thu 1 Oct 2026). Two declared ADDITIONS the canary carries that ci.yml's
#: `test` job does not have at all -- the inverse of CI_ONLY_STEPS/PYTEST_BUDGET_
#: SUBSTITUTION above, which declare what ci.yml has and the canary omits or shrinks.
#: Self-hosted runners get both for free (a provisioned host, MUX_FIXTURE_HOST); a
#: vendor VM is a cold ephemeral image and must never receive the real external
#: fixture host, so it needs its own pnpm and an explicit opt-in to skip the fixtures
#: it cannot have. drift_problems() subtracts exactly these two before comparing the
#: rest, so anything else added, removed, or moved still fails the comparison.
CANARY_ONLY_PNPM_STEP_NAME = "Enable pinned pnpm (vendor VMs have no self-hosted preinstall)"
CANARY_ONLY_ENV: dict[str, str] = {"MDT_ALLOW_MISSING_FIXTURES": "1"}


STATUS_FUNCTIONS = re.compile(r"\b(always|cancelled|failure|success)\s*\(")


def load_workflow(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text())
    # PyYAML reads the bare key `on` as boolean True.
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def allowed_owners(config: dict[str, Any]) -> list[str]:
    """The owners of the config's target repositories: the only ones the gate may run under."""
    return sorted({v["target_repo"].split("/")[0] for v in config["vendors"].values()})


def expected_guard(config: dict[str, Any]) -> str:
    return " || ".join(f"github.repository_owner == '{owner}'" for owner in allowed_owners(config))


EXPECTED_GUARD = expected_guard(CONFIG)


def owner_guard_problems(doc: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """The gate runs only under the target owners, and nothing runs past a skipped gate."""
    problems = []
    jobs = doc["jobs"]
    gate_if = str(jobs.get(GATE_JOB, {}).get("if", "")).strip()
    expected = expected_guard(config)
    if gate_if not in (expected, f"${{{{ {expected} }}}}"):
        problems.append(f"{GATE_JOB} if is {gate_if!r}, expected exactly {expected!r}")
    for job_id, job in jobs.items():
        if job_id == GATE_JOB:
            continue
        needs = job.get("needs")
        needs = [needs] if isinstance(needs, str) else list(needs or [])
        if GATE_JOB not in needs:
            problems.append(
                f"{job_id} does not need {GATE_JOB}, so it runs when the gate is skipped"
            )
        if STATUS_FUNCTIONS.search(str(job.get("if", ""))):
            problems.append(f"{job_id} if {job.get('if')!r} can run past a skipped gate")
    return problems


def gate_runs_under(doc: dict[str, Any], owner: str) -> bool:
    """Evaluate the gate's condition for `owner` with the expression evaluator below."""
    gate_if = str(doc["jobs"][GATE_JOB].get("if", "")).strip()
    return evaluate_condition(gate_if, {"github.repository_owner": owner})


_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>\|\||&&|==|!=|!|\(|\)|,)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_.-]*))"
)


def evaluate_condition(expr: str, context: dict[str, str]) -> bool:
    text = expr.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text.rstrip()):
        match = _TOKEN.match(text, pos)
        assert match, f"unsupported expression at {text[pos:]!r}"
        kind = match.lastgroup
        assert kind is not None, f"unnamed token at {text[pos:]!r}"
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    return bool(_Parser(tokens, context).parse())


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], context: dict[str, str]) -> None:
        self.tokens, self.context, self.i = tokens, context, 0

    def parse(self) -> Any:
        value = self._binary(("||",), lambda: self._binary(("&&",), self._comparison))
        assert self.i == len(self.tokens), f"trailing tokens {self.tokens[self.i :]}"
        return value

    def _peek(self) -> str | None:
        return self.tokens[self.i][1] if self.i < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def _binary(self, ops: tuple[str, ...], operand: Callable[[], Any]) -> Any:
        value = operand()
        while self._peek() in ops:
            op = self._take()[1]
            right = operand()
            value = (value or right) if op == "||" else (value and right)
        return value

    def _comparison(self) -> Any:
        left = self._unary()
        while self._peek() in ("==", "!="):
            op = self._take()[1]
            right = self._unary()
            left = (left == right) if op == "==" else (left != right)
        return left

    def _unary(self) -> Any:
        if self._peek() == "!":
            self._take()
            return not self._unary()
        kind, value = self._take()
        if value == "(":
            inner = self._binary(("||",), lambda: self._binary(("&&",), self._comparison))
            assert self._take()[1] == ")"
            return inner
        if kind == "str":
            return value[1:-1].replace("''", "'")
        if value == "startsWith":
            assert self._take()[1] == "("
            haystack = self._binary(("||",), self._comparison)
            assert self._take()[1] == ","
            needle = self._binary(("||",), self._comparison)
            assert self._take()[1] == ")"
            return str(haystack).lower().startswith(str(needle).lower())
        if value in ("true", "false"):
            return value == "true"
        assert kind == "name" and self._peek() != "(", f"unsupported function {value!r}"
        assert value in self.context, f"no value for context {value!r}"
        return self.context[value]


def trigger_problems(doc: dict[str, Any]) -> list[str]:
    problems = []
    on = doc["on"]
    if set(on) != {"push", "workflow_dispatch"}:
        problems.append(f"triggers are {sorted(on)}, expected exactly push and workflow_dispatch")
    push = on.get("push") or {}
    if push.get("branches") != ["canary/**"] or set(push) != {"branches"}:
        problems.append(f"push is {push!r}, expected branches ['canary/**'] only")
    return problems


def fallback_problems(doc: dict[str, Any]) -> list[str]:
    problems = []
    shard = doc["jobs"][SHARD_JOB]
    if shard.get("runs-on") != EXPECTED_RUNS_ON:
        problems.append(f"runs-on is {shard.get('runs-on')!r}, expected {EXPECTED_RUNS_ON!r}")
    matrix = shard.get("strategy", {}).get("matrix", {})
    if matrix.get("vendor") != EXPECTED_VENDOR_MATRIX:
        problems.append(
            f"matrix vendor is {matrix.get('vendor')!r}, expected {EXPECTED_VENDOR_MATRIX!r}"
        )
    if {"include", "exclude"} & set(matrix):
        problems.append("matrix include/exclude could add a vendor the gate did not allow")
    return problems


def permission_problems(doc: dict[str, Any], text: str) -> list[str]:
    problems = []
    if doc.get("permissions") != {"contents": "read"}:
        problems.append(f"top-level permissions are {doc.get('permissions')!r}")
    for job_id, job in doc["jobs"].items():
        for scope, level in (job.get("permissions") or {}).items():
            if level != "read":
                problems.append(f"{job_id} grants {scope}: {level}")
        for step in job.get("steps", []):
            is_checkout = str(step.get("uses", "")).startswith("actions/checkout@")
            if is_checkout and (step.get("with") or {}).get("persist-credentials") is not False:
                problems.append(f"{job_id} checkout persists credentials")
    if re.search(r"\bsecrets\.[A-Za-z_]", text):
        problems.append("the workflow references secrets")
    return problems


def _canary_expected_steps(ci_doc: dict[str, Any]) -> list[dict[str, Any]]:
    steps = copy.deepcopy(ci_doc["jobs"]["test"]["steps"])
    names = [s.get("name") for s in steps]
    for name in CI_ONLY_STEPS:
        assert name in names, f"declared CI-only step {name!r} no longer exists in ci.yml"
    # The omitted steps must be the TRAILING ones, so nothing the verdict depends on runs
    # after a point the canary stops at.
    assert names[-len(CI_ONLY_STEPS) :] == list(CI_ONLY_STEPS), names[-len(CI_ONLY_STEPS) :]
    kept = steps[: -len(CI_ONLY_STEPS)]
    old, new = PYTEST_BUDGET_SUBSTITUTION
    pytest_step = next(s for s in kept if s.get("name") == CONFIG["pytest_step_name"])
    assert pytest_step["run"].count(old) == 1, "ci.yml's shard budget line moved"
    pytest_step["run"] = pytest_step["run"].replace(old, new)
    for key in CI_ONLY_PYTEST_KEYS:
        assert key in pytest_step, f"declared CI-only pytest key {key!r} no longer in ci.yml"
        del pytest_step[key]
    return kept


def drift_problems(ci_doc: dict[str, Any], canary_doc: dict[str, Any]) -> list[str]:
    problems = []
    expected = _canary_expected_steps(ci_doc)
    actual = list(canary_doc["jobs"][SHARD_JOB]["steps"])

    # Subtract the one declared step ADDITION before the positional comparison below,
    # so its presence (anywhere) is required but its position is not pinned, while an
    # absent, duplicated, or renamed copy still fails loudly.
    pnpm_indices = [i for i, s in enumerate(actual) if s.get("name") == CANARY_ONLY_PNPM_STEP_NAME]
    if len(pnpm_indices) != 1:
        problems.append(
            f"canary carries {len(pnpm_indices)} steps named "
            f"{CANARY_ONLY_PNPM_STEP_NAME!r}, expected exactly 1"
        )
    else:
        del actual[pnpm_indices[0]]

    if len(expected) != len(actual):
        problems.append(f"canary has {len(actual)} shard steps, ci.yml implies {len(expected)}")
    for index, (want, got) in enumerate(zip(expected, actual, strict=False)):
        if want != got:
            problems.append(
                f"step {index} ({want.get('name') or want.get('uses')}) differs from ci.yml"
            )

    ci_env = dict(ci_doc["jobs"]["test"].get("env") or {})
    canary_env = dict(canary_doc["jobs"][SHARD_JOB].get("env") or {})
    for key, value in CANARY_ONLY_ENV.items():
        actual_value = canary_env.pop(key, None)
        if actual_value != value:
            problems.append(
                f"shard env {key!r} is {actual_value!r}, expected declared addition {value!r}"
            )
    if ci_env != canary_env:
        problems.append("shard env differs from ci.yml's test job env beyond the declared addition")
    return problems


def fixture_skip_flag_problems(config: dict[str, Any], canary_doc: dict[str, Any]) -> list[str]:
    """The report's unequal-work cap (Sol P1 on 0e7388c6f) keys on the config flag, so the
    flag must say exactly what the workflow env does, in both directions."""
    env = canary_doc["jobs"][SHARD_JOB].get("env") or {}
    skips = env.get("MDT_ALLOW_MISSING_FIXTURES") == "1"
    if config["vendor_skips_external_fixtures"] is not skips:
        return [
            f"ci/runner-canary.json vendor_skips_external_fixtures is "
            f"{config['vendor_skips_external_fixtures']!r}, but the canary shard env "
            f"{'sets' if skips else 'does not set'} MDT_ALLOW_MISSING_FIXTURES=1"
        ]
    return []


def _branch_filter_matches(pattern: str, branch: str) -> bool:
    """GitHub's branch filter glob: `**` crosses `/`, `*` does not. `!` negation refused."""
    assert not pattern.startswith("!"), f"negated filter {pattern!r} is not modelled"
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, branch) is not None


def conditions_for_every_var(expr: str, event: str, ref: str) -> list[bool]:
    """The job condition under every value its `vars.*` switches can take."""
    names = sorted(set(re.findall(r"\bvars\.[A-Za-z0-9_]+", expr)))
    results = []
    for index in range(2 ** len(names)):
        ctx = {"github.event_name": event, "github.ref": ref}
        ctx |= {n: ("true" if index >> k & 1 else "") for k, n in enumerate(names)}
        results.append(evaluate_condition(expr, ctx))
    return results


def _triggers(on: str | list[str] | dict[str, Any]) -> dict[str, Any]:
    """`on:` in any of its three spellings, as a mapping of event to filter."""
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return on


def canary_push_problems(workflows: dict[str, dict[str, Any]]) -> list[str]:
    """Workflows a push of `canary/<sha>` would START a job in, other than the canary.

    A new branch fires `push` (filtered by `branches`) and `create` (unfilterable, so each
    subscribed job must refuse the ref in its own condition).
    """
    branch = CANARY_REF.removeprefix("refs/heads/")
    problems = []
    for name, doc in workflows.items():
        on = _triggers(doc["on"])
        if "push" in on:
            push = on["push"] or {}
            if "branches" not in push:
                problems.append(f"{name}: push has no branches filter, so it matches {branch}")
            elif any(_branch_filter_matches(p, branch) for p in push["branches"]):
                problems.append(f"{name}: push branches {push['branches']} match {branch}")
        if "create" in on:
            for job_id, job in doc["jobs"].items():
                condition = job.get("if")
                if condition is None or any(
                    conditions_for_every_var(str(condition), "create", CANARY_REF)
                ):
                    problems.append(f"{name}/{job_id}: a `create` of {branch} starts this job")
    return problems


def vendor_label_problems(texts: dict[str, str], config: dict[str, Any]) -> list[str]:
    return [
        f"{name} names vendor label {v['label']!r}"
        for name, text in texts.items()
        for v in config["vendors"].values()
        if v["label"] in text
    ]


def other_workflows() -> dict[str, Path]:
    paths = {p.name: p for p in sorted(WORKFLOW_DIR.glob("*.yml")) if p != CANARY_PATH}
    assert len(paths) >= 20, "control: the workflow directory reads as expected"
    return paths


def rendered_names(job: dict[str, Any], values: dict[str, list[Any]]) -> set[str]:
    names = {str(job.get("name", ""))}
    for key, options in values.items():
        names = {
            n.replace(f"${{{{ matrix.{key} }}}}", str(option)) for n in names for option in options
        }
    return names
