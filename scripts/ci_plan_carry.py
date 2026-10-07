"""What a GLOBAL pytest helper carries: the derivation behind `helper_carried` in
ci/test-scopes.yml.

Supersedes: ``helper_carried_scopes`` in ``scripts/ci_plan.py``, moved here, not copied.

Split out of ci_plan.py, which crossed the file size ceiling in round 12. This module depends
on ci_plan.py (its scopes, owner index and source closure), never the reverse: plan time
reads the committed list, and this is what regenerates it and what the guard compares it to.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from scripts.ci_plan import (
    REPO,
    Config,
    _consumed_scopes,
    _source_reachability,
    _test_owner_index,
    matches,
)
from scripts.ci_plan_sources import (
    _read_source,
    imported_test_helpers,
    is_test_module,
    pytest_inputs,
)


def helper_carried_scopes(config: Config, root: Path = REPO) -> frozenset[str]:
    """Scopes whose change must run EVERYTHING, because a GLOBAL pytest helper imports them.

    A helper matched by `always` is not a test and does not run; it is imported by suites
    across the tree, including scope-owned suites that are not always run. Its imports are
    therefore a dependency of those suites, and its consumers cannot be resolved from its own
    text, so the attribution is the widest honest one.

    Closed over the source graph for the same reason `observed_dependents` is: a helper that
    imports scope A also depends on everything A imports.

    Round 12 narrows ONE case, read off the consumers rather than assumed: a helper that is
    not a `conftest.py` and is imported ONLY by test modules that run in every plan carries
    nothing, because every plan already runs the suites that exercise what it imports. Two
    root helpers (`tests/quality_gate_jscpd.py`, `tests/ci_cost_guard_workflow_reader.py`)
    consumed by three root test modules were carrying the `scripts` scope, whose closure is
    the whole tree. A helper nobody imports, one imported by a `conftest.py` or another
    helper, or one a scope-owned suite reaches keeps the widest attribution.
    """
    reaches = _source_reachability(config, root)
    owner_of = _test_owner_index(config)
    inputs = {
        (file.relative_to(root) if file.is_absolute() else file).as_posix(): file
        for file in pytest_inputs(root / "tests")
    }
    importers: dict[str, set[str]] = {relative: set() for relative in inputs}
    for relative, file in inputs.items():
        for name in imported_test_helpers(_read_source(file, relative), relative):
            target = name.replace(".", "/") + ".py"
            if target in importers and target != relative:
                importers[target].add(relative)
    carried: set[str] = set()
    for relative, file in inputs.items():
        if owner_of(relative) is not None or not matches(relative, config.always):
            continue
        if is_test_module(relative):
            continue
        if _consumed_only_by_always_run_tests(relative, importers[relative], owner_of, config):
            continue
        for scope in _consumed_scopes(_read_source(file, relative), relative, config):
            carried |= reaches[scope.name]
    return frozenset(carried)


def _consumed_only_by_always_run_tests(
    helper: str, consumers: set[str], owner_of: Callable[[str], str | None], config: Config
) -> bool:
    """True when every plan already runs every suite that reaches `helper`, so its imports
    need no carrying. Empty consumers is NOT that: a helper reached through `pytest_plugins`,
    a fixture name or nothing at all cannot be placed, and unplaceable stays widest."""
    if helper.rsplit("/", 1)[-1] == "conftest.py" or not consumers:
        return False
    return all(
        is_test_module(c) and owner_of(c) is None and matches(c, config.always) for c in consumers
    )
