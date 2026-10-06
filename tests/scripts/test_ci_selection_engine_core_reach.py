"""PR-head selection reaches tests/engine_core from the webui modules engine_core imports.

`apps/engine_core` imports webui route callables and dependencies DIRECTLY (not through
FastAPI DI): `routes.health.health`, `routes.rb_assets.get_track_anlz`,
`routes.auth.signed_in_user`, `deps.get_read_state`, ... A `def` -> `async def` change to
one of them breaks the packaged engine while the dev preview, which runs
`apps.webui.server`, stays green (#5494). `tests/engine_core/test_contract_rev.py` is the
test that goes red, so a change to any of those modules must select it.

Today that reach is DERIVED, not hand-mapped: `tests/engine_core/conftest.py` imports
through webui, so the conftest-reach rule selects every engine_core test. These tests pin
it against THIS repository, and the webui module list is read from engine_core's own
imports by AST, so a new direct import is covered without editing this file.

[if] a webui module engine_core imports stops selecting tests/engine_core [then] fail, [else stop].
"""

from __future__ import annotations

import ast
from functools import cache

import pytest
import yaml

from scripts.affected_tests import REPO, reverse_reach
from scripts.ci_test_selection_rules import Graph, select_tests

pytestmark = pytest.mark.requirement("DEVOPS-20")

CI = REPO / ".github" / "workflows" / "ci.yml"
CONTRACT_TEST = "tests/engine_core/test_contract_rev.py"
ENGINE_CONFTEST = "tests.engine_core.conftest"
WEBUI_SERVER = "apps.webui.server"
#: Known direct imports; the derived set must keep containing them, so the AST
#: derivation cannot silently shrink to nothing and pass.
KNOWN_DIRECT = {
    "apps.webui.server.routes.health",
    "apps.webui.server.routes.rb_assets",
    "apps.webui.server.routes.auth",
    "apps.webui.server.deps",
}


def _shard_ignores() -> list[str]:
    """The `--ignore` paths of the pytest fast lane shard job, as CI passes them."""
    steps = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["test"]["steps"]
    runs = [s.get("run") or "" for s in steps if "ci_test_selection select" in (s.get("run") or "")]
    assert len(runs) == 1, f"expected one shard selection step, found {len(runs)}"
    tokens = runs[0].replace("\\", " ").split()
    return [t.removeprefix("--ignore=") for t in tokens if t.startswith("--ignore=")]


@cache
def _graph() -> Graph:
    return Graph.load(REPO)


def _webui_modules_engine_core_imports(graph: Graph) -> set[str]:
    """Every apps.webui.server module an apps/engine_core file imports, resolved to a module."""
    found: set[str] = set()
    for path in sorted((REPO / "apps" / "engine_core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                if name.startswith(WEBUI_SERVER) and name in graph.path_of:
                    found.add(name)
    return found


def test_a_health_route_change_selects_the_engine_contract_test():
    """[if] a routes/health.py change skips test_contract_rev [then] fail, [else stop]."""
    selection = select_tests(["apps/webui/server/routes/health.py"], _shard_ignores())
    assert CONTRACT_TEST in selection.modules, selection.reasons.get(CONTRACT_TEST)


def test_an_rb_assets_change_selects_engine_core_tests():
    """[if] a routes/rb_assets.py change skips engine_core tests [then] fail, [else stop]."""
    selection = select_tests(["apps/webui/server/routes/rb_assets.py"], _shard_ignores())
    assert CONTRACT_TEST in selection.modules
    assert "tests/engine_core/test_audio_engine.py" in selection.modules


def test_every_webui_module_engine_core_imports_reaches_the_engine_conftest():
    """[if] a webui module engine_core imports misses its conftest [then] fail, [else stop]."""
    graph = _graph()
    derived = _webui_modules_engine_core_imports(graph)
    assert derived >= KNOWN_DIRECT, sorted(KNOWN_DIRECT - derived)
    missing = sorted(m for m in derived if ENGINE_CONFTEST not in reverse_reach({m}, graph.importers))
    assert not missing, f"engine_core imports these but no engine_core test would run: {missing}"


def test_engine_core_is_not_lane_ignored():
    """[if] the shard lane ignores tests/engine_core [then] fail, [else stop]."""
    assert not any(CONTRACT_TEST.startswith(i.rstrip("/")) for i in _shard_ignores())
