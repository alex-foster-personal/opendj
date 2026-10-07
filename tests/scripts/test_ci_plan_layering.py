"""The layering instrument: scope edges, cycles and what a global helper's closure reaches.

[if] a scope cycle goes unreported, or a cut edge is still reached [then] fail, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci_plan import Config, Scope
from scripts.ci_plan_layering import closure, helper_direct_scopes, scope_edges, strongly_connected

pytestmark = pytest.mark.requirement("OPS-16")


def _tree(tmp_path: Path) -> Config:
    for rel, text in (
        ("apps/a/x.py", "import apps.b.y\n"),
        ("apps/b/y.py", "import apps.a.x\n"),
        ("apps/c/z.py", "import apps.a.x\n"),
        ("tests/rig.py", "from apps.c import z\n"),
        ("tests/c/test_z.py", "from tests.rig import z\n"),
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return Config(
        (),
        ("tests/*.py",),
        (
            Scope("a", ("apps/a/",), ("tests/a/",)),
            Scope("b", ("apps/b/",), ("tests/b/",)),
            Scope("c", ("apps/c/",), ("tests/c/",)),
        ),
    )


def test_edges_carry_their_sites_and_the_cycle_is_found(tmp_path: Path) -> None:
    config = _tree(tmp_path)
    edges = scope_edges(config, tmp_path)
    assert edges == {
        ("a", "b"): ["apps/a/x.py -> apps.b.y"],
        ("b", "a"): ["apps/b/y.py -> apps.a.x"],
        ("c", "a"): ["apps/c/z.py -> apps.a.x"],
    }
    assert strongly_connected(["a", "b", "c"], edges)[0] == ["a", "b"]


def test_a_helper_closure_shrinks_when_an_edge_is_cut(tmp_path: Path) -> None:
    config = _tree(tmp_path)
    edges = scope_edges(config, tmp_path)
    direct = helper_direct_scopes(config, tmp_path)
    assert direct == {"tests/rig.py": ["c"]}
    assert closure(direct["tests/rig.py"], edges) == {"a", "b", "c"}
    assert closure(direct["tests/rig.py"], set(edges) - {("c", "a")}) == {"c"}
