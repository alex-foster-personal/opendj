"""Reading the workflow files on disk the way the cost guard would price them.

Shared by tests/test_ci_cost_guard.py, which drives these readers over
hand-written expressions, and tests/test_ci_cost_guard_workflow_coverage.py,
which runs them over `.github/workflows` and asserts on the arithmetic that
comes out. Both modules ask the same question - what would GitHub bill for this
definition - so the answer lives in one place rather than being restated twice.

Every reader here is FAIL-CLOSED by design. A shape none of them understands
raises rather than returning a plausible number, because a guessed runner or a
skipped disjunct reads as a small, cheap answer that satisfies every
containment check downstream, which is the exact failure the suites exist to
catch.

This module holds no tests of its own and is deliberately not named `test_*`,
so pytest imports it only when a test module asks for it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from scripts.ci_cost_guard import infer_standard_sku

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
# The cost guard is the `assess` job of error-sink.yml since Tue 22 Sep 2026
# (one observer run per completion, issue #2196).
GUARD = WORKFLOW_DIR / "error-sink.yml"
MACOS_DESKTOP_COMPILE = WORKFLOW_DIR / "macos-desktop-compile.yml"
MACOS_PACKAGING = WORKFLOW_DIR / "macos-packaging.yml"
MACOS_NATIVE_COMPANION = WORKFLOW_DIR / "macos-native-companion.yml"


# ----- the workflow definitions themselves ----------------------------------


def workflow_docs() -> dict[str, dict]:
    """Every workflow definition on disk, keyed by the `name:` the guard sees.

    Both suffixes GitHub will execute, not just `.yml`: Codex pointed out on
    #713 that a `.yaml` workflow whose ceiling exceeds the threshold stays
    absent from the watch list while the repository-wide assertion in
    tests/test_ci_cost_guard_workflow_coverage.py still passes, because the glob
    never loads it.
    """

    out: dict[str, dict] = {}
    seen: dict[str, Path] = {}
    for path in sorted(p for pat in ("*.yml", "*.yaml") for p in WORKFLOW_DIR.glob(pat)):
        doc = yaml.safe_load(path.read_text())
        name = doc.get("name", path.stem)
        # Refuse a collision rather than let the later file win. GitHub happily
        # runs two workflows sharing a `name:`, and this dict keys on the name
        # the guard sees, so a silent overwrite prices only one of them: an
        # expensive definition can be hidden behind a cheap namesake and the
        # shared name left off the watch list with everything still green.
        # Reachable because the glob above deliberately reads BOTH suffixes.
        assert name not in seen, (
            f"{path.name} and {seen[name].name} both declare `name: {name}`, so "
            "pricing one of them silently drops the other. Rename one, or price "
            "every document per name."
        )
        seen[name] = path
        out[name] = doc
    return out


#: The runner-switch expression `runs-on` carries since CI moved to self-hosted
#: runners: `${{ fromJSON(vars.CI_RUNS_ON_X || '"ubuntu-latest"') }}`. Only this
#: exact shape is read; see `runner_labels`.
RUNNER_SWITCH = re.compile(
    r"\$\{\{\s*fromJSON\(\s*vars\.[A-Z0-9_]+\s*\|\|\s*'(?P<fallback>.+?)'\s*\)\s*\}\}"
)
CHAINED_RUNNER_SWITCH = re.compile(
    r"\$\{\{\s*fromJSON\(\s*(?P<inner>.*?)\)\s*\}\}"
)
#: First disjunct of ci.yml job `test` runs-on (ADR-0041 main-fix reserve). Pinned
#: in tests/scripts/test_ci_main_fix_runner_reserve.py EXPECTED_RUNS_ON.
MAIN_FIX_RUNNER_GUARD_PREFIX = (
    "(github.event_name == 'pull_request' && "
    "contains(github.event.pull_request.labels.*.name, 'ci:trunk-repair')) "
    "&& vars.CI_RUNS_ON_MAIN_FIX"
)
_JSON_LITERAL_DISJUNCT = re.compile(r"^'(.+)'\s*$")
_VARS_DISJUNCT = re.compile(r"^vars\.[A-Z0-9_]+$")


def runner_labels(job_id: str, runs_on: object) -> list[str]:
    """The labels to price `runs-on` at, resolving the runner-switch expression.

    `runs-on` stopped being a literal label when runner selection became a repo
    variable, so the same workflow file bills at two different rates depending
    on a value that is not in the repository. The ceiling has to be the WORST
    case the switch can select, and the only candidate this file can read is
    the literal fallback baked into the `||`.

    That is also the expensive one, which is why resolving to it is safe rather
    than convenient: `infer_standard_sku` prices self-hosted at $0, so pricing
    the hosted fallback leaves every ceiling exactly where it stood before the
    switch existed and cannot drop a workflow off the watch list. A variable
    pointing at some OTHER hosted SKU is priced at RUN time by `price_jobs`,
    which reads the labels the API reports and never sees this expression.

    Fail-closed on anything else, for the same reason `event_set` refuses a
    clause it cannot read: an expression resolved by guesswork could name a
    cheap runner for an expensive job and the missing alert would look green.
    """
    if isinstance(runs_on, str):
        labels = [runs_on]
    elif isinstance(runs_on, list):
        labels = [str(label) for label in runs_on]
    elif runs_on is None:
        labels = []
    else:
        raise AssertionError(
            f"{job_id} has a runs-on of type {type(runs_on).__name__}, which is "
            "neither a label, a list of labels nor absent, so it cannot be priced"
        )
    if not any("${{" in label for label in labels):
        return labels
    assert len(labels) == 1, (
        f"{job_id} mixes an expression with literal labels ({labels}), so which "
        "runner it selects cannot be read here"
    )
    expr = labels[0].strip()
    match = RUNNER_SWITCH.fullmatch(expr)
    if match:
        fallback = json.loads(match.group("fallback"))
        return [fallback] if isinstance(fallback, str) else list(fallback)

    chained = CHAINED_RUNNER_SWITCH.fullmatch(expr)
    assert chained, (
        f"{job_id} runs on the expression {labels[0]!r}, which this reader cannot "
        "price. Only the `fromJSON(vars.X || '<json>')` runner switch and chained "
        "`fromJSON(vars.A || vars.B || ... || '<json>')` fallbacks are "
        "understood; widen this deliberately rather than letting an unreadable "
        "expression be priced by guesswork."
    )
    inner = chained.group("inner")
    if "inputs." in inner:
        raise AssertionError(
            f"{job_id} runs on the expression {labels[0]!r}, which this reader cannot "
            "price: inputs.* disjuncts are not readable here"
        )
    disjuncts = top_level_disjuncts(inner)
    assert disjuncts, (
        f"{job_id} runner expression {expr!r} has no disjuncts this reader can read"
    )
    literal_match = _JSON_LITERAL_DISJUNCT.fullmatch(disjuncts[-1])
    assert literal_match, (
        f"{job_id} runs on the expression {labels[0]!r}, which this reader cannot "
        "price: the final disjunct is not a literal JSON fallback"
    )
    for disjunct in disjuncts[:-1]:
        trimmed = disjunct.strip()
        if trimmed == MAIN_FIX_RUNNER_GUARD_PREFIX:
            continue
        assert _VARS_DISJUNCT.fullmatch(trimmed), (
            f"{job_id} runs on the expression {labels[0]!r}, which this reader cannot "
            f"price: disjunct {disjunct!r} is neither vars.* nor the ADR-0041 "
            "main-fix guard prefix"
        )
    fallback = json.loads(literal_match.group(1))
    return [fallback] if isinstance(fallback, str) else list(fallback)


def matrix_runs(job_id: str, job: dict) -> int:
    """How many billed runs one job definition expands to.

    A `strategy.matrix` job bills once PER COMBINATION, so a ceiling that
    counted it once would understate the workflow by a factor of the shard
    count. Only the plain shape is read (every key a literal list, no
    `include` / `exclude`, no expression); anything else is refused rather
    than guessed, in the same fail-closed spirit as `runner_labels`.
    """
    strategy = job.get("strategy")
    if strategy is None:
        return 1
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), (
        f"{job_id} has a strategy this reader cannot read: {strategy!r}"
    )
    assert not ({"include", "exclude"} & matrix.keys()), (
        f"{job_id} uses matrix include/exclude, which this ceiling cannot count"
    )
    runs = 1
    for key, values in matrix.items():
        assert isinstance(values, list) and values, (
            f"{job_id} matrix key {key!r} is not a literal list: {values!r}"
        )
        runs *= len(values)
    return runs


def called_workflow_path(job_id: str, uses: str) -> Path:
    """The local workflow a `uses:` job calls, refusing anything remote.

    A remote reusable workflow (`owner/repo/.github/workflows/x.yml@ref`) is
    not readable from this checkout, so its ceiling cannot be computed and
    must not be assumed cheap.
    """
    assert uses.startswith("./.github/workflows/"), (
        f"{job_id} calls {uses!r}, which this reader cannot price: only local "
        "reusable workflows under ./.github/workflows/ can be read from this "
        "checkout, and an unpriceable job must fail rather than read as free"
    )
    path = WORKFLOW_DIR.parents[1] / uses[2:]
    assert path.is_file(), f"{job_id} calls {uses!r}, which does not exist"
    return path


def job_ceiling_usd(job_id: str, job: dict, _depth: int = 0) -> float:
    """One job's worst-case cost, priced through the guard's own SKU table.

    A matrix job is priced once per combination: the fast pytest lane shards
    across runners, and each shard is its own billed job with its own timeout.

    A CALLER job (`uses: ./.github/workflows/x.yml`) has no `runs-on` and no
    `timeout-minutes` of its own: GitHub forbids both on a reusable-workflow
    call. Its cost is the called workflow's own ceiling, so it is priced by
    recursion rather than by the assertion below, which would otherwise
    classify every caller as unpriceable. periodic-checks.yml calls
    full-ci.yml this way.
    """
    uses = job.get("uses")
    if isinstance(uses, str):
        assert _depth < 4, f"{job_id}: reusable-workflow nesting is too deep to price"
        called = yaml.safe_load(called_workflow_path(job_id, uses).read_text())
        return sum(
            job_ceiling_usd(f"{job_id}->{i}", j, _depth + 1)
            for i, j in (called.get("jobs") or {}).items()
        )

    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int), (
        f"{job_id} has no explicit timeout-minutes, so its ceiling is "
        "GitHub's 360-minute default and this arithmetic is meaningless"
    )
    labels = runner_labels(job_id, job.get("runs-on"))
    sku = infer_standard_sku(labels)
    assert sku is not None, f"{job_id} runs on {labels}, which has no known rate"
    # One billed minute MORE than the timeout, because that is what production
    # would charge. `price_jobs` bills `max(1, math.ceil(seconds / 60))`, so a
    # job cancelled at a 30-minute timeout whose recorded duration runs even
    # fractionally past the mark bills 31 minutes, not 30. Treating the timeout
    # as an exact billed ceiling understates every workflow by up to one minute,
    # and a workflow sitting exactly ON the threshold is then classified as
    # unable to trip and dropped from the watch list. Codex found this on #713:
    # Windows Parity priced at exactly $0.30 against a `> $0.30` alert.
    return matrix_runs(job_id, job) * (timeout + 1) * sku.rate_usd_per_minute


def ceiling_usd(doc: dict) -> float:
    """Worst-case cost of one run: EVERY job at its own timeout.

    Every job, including ones behind an `if`, because a ceiling that ignores a
    conditional job is exactly the mistake this test exists to catch: the
    guard's comment priced E2E at its 30-minute gate and missed the 45-minute
    nightly `extended` job sitting beside it.

    Priced through the production `infer_standard_sku`, so a runner the guard
    could not price is a runner this cannot price either, and the missing
    number fails the test instead of quietly reading as cheap.
    """
    return sum(job_ceiling_usd(i, j) for i, j in (doc.get("jobs") or {}).items())


# ----- the conditions that decide which jobs run ----------------------------


def top_level_disjuncts(condition: str) -> list[str]:
    """`condition` split on `||` at bracket depth zero.

    A plain `str.split("||")` would cut inside a parenthesized clause and hand
    back fragments, and a fragment reads as a weaker claim than the clause it
    came from - the same fail-open shape `event_set` refuses below.
    """
    stripped = re.sub(r"\s+", " ", condition).strip()
    parts, depth, start = [], 0, 0
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0 and stripped[index : index + 2] == "||":
            parts.append(stripped[start:index].strip())
            index += 2
            start = index
            continue
        index += 1
    parts.append(stripped[start:].strip())
    return [part for part in parts if part]


def event_set(condition: str, variable: str) -> set[str]:
    """The event names `condition` admits. Fail-closed: EVERY disjunct counts.

    Only one shape is accepted: a disjunction in which every clause is
    `<variable> == \'<name>\'`. An earlier revision skipped past clauses that
    did not mention `variable`, and Codex called that on #713: a disjunct like
    `github.ref == \'refs/heads/main\'` admits every event the workflow
    triggers on, including `push`, while the parser silently returned
    `{\'schedule\'}` and the containment in
    tests/test_ci_cost_guard_workflow_coverage.py still passed. A clause this
    function cannot read may widen the set arbitrarily, so it must refuse
    rather than skip - an uncomputable set returned as a small one satisfies
    every containment, which is the failure it exists to prevent.
    """
    stripped = re.sub(r"\s+", " ", condition).strip()

    # ONE widening, and only one: a leading `!cancelled() &&` around a
    # parenthesized disjunction is stripped. A status function decides whether
    # a run happens at all, so it can REMOVE runs and can never add an event -
    # which is the only property that makes widening a fail-closed parser safe.
    # e2e.yml's `extended` gained that guard when it was ordered after `gate`
    # (#1014). Any other unreadable clause still refuses below.
    guarded = re.fullmatch(r"!cancelled\(\) && \((.+)\)", stripped)
    if guarded:
        stripped = guarded.group(1)

    events: set[str] = set()
    # Depth-aware, so a parenthesized clause is never cut into fragments that
    # each read as a weaker claim than the clause they came from.
    for clause in top_level_disjuncts(stripped):
        match = re.fullmatch(rf"{re.escape(variable)} == \'([A-Za-z_]+)\'", clause)
        assert match, (
            f"cannot compute the event set: clause {clause!r} is not a "
            f"`{variable} == '<name>'` comparison, so the events it admits "
            f"are unknown and may be all of them. Widen the parser "
            f"deliberately, or gate the job on event names only."
        )
        events.add(match.group(1))
    return events


_UNPRICED_PREFIX = re.compile(r"^github\.event\.workflow_run\.name != '([^']+)' && ")


def unpriced_names(condition: str) -> tuple[set[str], str]:
    """Workflows the guard's gate excludes by name, and the gate that remains.

    The only shape read is a leading run of `github.event.workflow_run.name !=
    '<workflow>' &&` clauses followed by one parenthesized group. A name
    exclusion can only REMOVE runs, so stripping it is fail-closed in the same
    way `event_set`'s `!cancelled()` widening is. Any other shape is left for
    the strict parsers below to refuse.
    """
    stripped = re.sub(r"\s+", " ", condition).strip()
    names: set[str] = set()
    while (match := _UNPRICED_PREFIX.match(stripped)):
        names.add(match.group(1))
        stripped = stripped[match.end():]
    if names:
        assert stripped.startswith("(") and stripped.endswith(")"), (
            f"a name exclusion must be followed by one parenthesized gate: {condition}"
        )
        stripped = stripped[1:-1].strip()
    return names, stripped


def e2e_priced_events(condition: str) -> set[str]:
    """The events the guard prices FOR E2E, read from its `assess` gate.

    The gate opens with `github.event.workflow_run.name != \'E2E\'`, which is
    the escape hatch for every other workflow and is what makes the remaining
    disjuncts E2E-specific. That one clause is matched exactly and removed;
    everything after it goes through the strict parser above, so a fourth
    disjunct of any other shape reddens this rather than being skipped.
    """
    _, stripped = unpriced_names(condition)
    escape = "github.event.workflow_run.name != 'E2E'"
    head, sep, tail = stripped.partition("||")
    assert head.strip() == escape and sep, (
        f"the guard's gate no longer opens with {escape!r}, so which of its "
        f"clauses are E2E-specific can no longer be read: {condition}"
    )
    return event_set(tail, "github.event.workflow_run.event")


def e2e_ceiling_on(event: str, doc: dict) -> float:
    """What an E2E run triggered by `event` can cost at worst.

    Every job that would run on that event, priced. A job with no `if` runs on
    every trigger the workflow declares; a gated one runs only on the events
    its condition admits, read by the fail-closed parser above.
    """
    total = 0.0
    for job_id, job in doc["jobs"].items():
        gate = job.get("if")
        if gate is None or event in event_set(gate, "github.event_name"):
            total += job_ceiling_usd(job_id, job)
    return total


def guard_threshold() -> float:
    match = re.search(r"--threshold\s+([0-9.]+)", GUARD.read_text())
    assert match, "the guard no longer passes --threshold; this reader cannot measure"
    return float(match.group(1))
