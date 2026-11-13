"""Issue #11466: /performance first-paint import deferrals (PR #12818)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "apps/webui/frontend"
DEFER_UNIT = FRONTEND / "tests/unit/performance-route-defer-imports.test.mjs"
BUILD_DIR = FRONTEND / "build"
BUNDLE_GATE = FRONTEND / "scripts/bundle-budget.mjs"


def _node_test(path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is not on PATH")
    proc = subprocess.run(
        [node, "--test", "--test-reporter=spec", str(path)],
        cwd=FRONTEND,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)


@pytest.mark.requirement("WEBUI-12818")
def test_performance_route_defers_lazy_only_imports() -> None:
    """[if] defer tests run [then] /performance has no banned static imports, [else stop]."""
    _node_test(DEFER_UNIT)


@pytest.mark.requirement("WEBUI-12818")
def test_built_performance_surface_stays_within_budget_gate() -> None:
    """[if] build/ exists [then] performance gzip is at or under its limit, [else stop]."""
    if not BUILD_DIR.is_dir():
        pytest.skip("production build missing; run pnpm build in apps/webui/frontend")
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is not on PATH")
    proc = subprocess.run(
        [node, str(BUNDLE_GATE), "--json"],
        cwd=FRONTEND,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    stdout = proc.stdout
    trailer = stdout.find("\n[OK]")
    if trailer != -1:
        stdout = stdout[:trailer]
    payload = json.loads(stdout)
    perf_bytes = payload["totals"]["performance"]["bytes"]
    perf_budget = next(b for b in payload["budgets"] if b["name"] == "performance")
    assert perf_bytes <= perf_budget["limit"], (
        f"performance {perf_bytes} gzip bytes exceeds limit {perf_budget['limit']}"
    )
