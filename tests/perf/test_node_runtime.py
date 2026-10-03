"""The resolved Node the PERFMODE-15 capture helpers launch (Codex P1 r4171125801, PR #4888)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.perf import node_runtime


@pytest.mark.requirement("PERFMODE-15")
def test_the_node_on_path_resolves_to_an_absolute_path_at_or_above_the_floor() -> None:
    """[if] PATH holds a Node at or above the frontend floor [then] its absolute path is returned and runs, [else stop]."""
    if shutil.which("node") is None:
        pytest.skip("UNAVAILABLE: node is not on PATH on this host")
    node = node_runtime.resolved_node()
    assert Path(node).is_absolute(), node
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30, check=True).stdout
    assert node_runtime.parse_node_version(version) >= node_runtime.node_floor()


@pytest.mark.requirement("PERFMODE-15")
def test_no_node_on_the_search_path_is_refused_by_name(tmp_path: Path) -> None:
    """[if] the search path holds no node [then] resolution exits naming PATH, never runs a bare node, [else stop]."""
    with pytest.raises(SystemExit, match="node is not on PATH"):
        node_runtime.resolved_node(str(tmp_path))


@pytest.mark.requirement("PERFMODE-15")
def test_a_node_below_the_floor_is_refused_and_one_at_it_is_not() -> None:
    """[if] Node is older than the frontend floor [then] it is refused, while the floor itself passes, [else stop]."""
    floor = node_runtime.node_floor()
    older = (floor[0], floor[1], floor[2] - 1) if floor[2] > 0 else (floor[0], floor[1] - 1, 99)
    refusal = node_runtime.node_version_refusal(older, floor)
    assert refusal is not None and "below the frontend floor" in refusal
    assert node_runtime.node_version_refusal(floor, floor) is None


@pytest.mark.requirement("PERFMODE-15")
def test_the_floor_is_read_from_the_frontend_engines_field() -> None:
    """[if] the frontend declares engines.node >=X.Y.Z [then] the floor is exactly X.Y.Z, [else stop]."""
    spec = node_runtime.json.loads(node_runtime._FRONTEND_PACKAGE.read_text(encoding="utf-8"))["engines"]["node"]
    assert spec.startswith(">=")
    assert node_runtime.node_floor() == node_runtime.parse_node_version(spec[2:])
