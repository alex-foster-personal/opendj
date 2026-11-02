"""Round 28: a suite that imports ANOTHER scope's test file depends on what that file imports.

Round 27 measured it at collection: `tests/sync/conftest.py` re-exports fixtures from
`tests/sets/conftest.py`, which imports `apps.sets`, and no file under `tests/sync/` names
`apps.sets`, so the import-text derivation gave `sets` no `sync` dependent. The real tree had two
such chains and 23 missing edges (`tests/genre_infer/` through `tests/webui/library_wheel_fixtures.py`
was the other).

[if] a scope's tests reach another scope's package only through its test files and no edge is derived [then] fail here, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci_plan import Config, Scope, observed_dependents
from scripts.ci_plan_sources import pytest_inputs

pytestmark = pytest.mark.requirement("OPS-16")

CONFIG = Config(
    (),
    ("tests/*.py",),
    (
        Scope("engine", ("apps/engine_core/",), ("tests/engine_core/",)),
        Scope("library", ("apps/library/",), ("tests/library/",)),
        Scope("core", ("apps/core/",), ("tests/core/",)),
    ),
)


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, text in {
        "apps/engine_core/thing.py": "from apps.core import base\n",
        "apps/core/base.py": "",
        "apps/library/shelf.py": "",
        "tests/engine_core/conftest.py": "from apps.engine_core.thing import Thing\n",
        "tests/engine_core/test_engine.py": "def test_engine() -> None: ...\n",
        "tests/library/test_shelf.py": "from apps.library import shelf\n",
        **files,
    }.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text)
    return tmp_path / "tests"


def test_a_conftest_that_reexports_another_scopes_fixtures_depends_on_its_imports(tmp_path: Path) -> None:
    root = _tree(
        tmp_path, {"tests/library/conftest.py": "from tests.engine_core.conftest import Thing  # noqa: F401\n"}
    )
    assert observed_dependents(CONFIG, pytest_inputs(root), root=tmp_path) == {
        "core": ("engine", "library"),  # closed over the source graph: engine imports core
        "engine": ("library",),
    }


def test_the_chain_is_followed_through_own_and_global_helpers(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "tests/library/test_use.py": "from tests.rig import build\n",
            "tests/rig.py": "from tests.engine_core.support import build  # noqa: F401\n",
            "tests/engine_core/support.py": "from tests.engine_core.conftest import Thing as build  # noqa: F401\n",
        },
    )
    assert observed_dependents(CONFIG, pytest_inputs(root), root=tmp_path) == {
        "core": ("engine", "library"),
        "engine": ("library",),
    }


def test_a_scope_importing_its_own_test_files_adds_no_edge(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"tests/engine_core/test_more.py": "from tests.engine_core.conftest import Thing\n"})
    assert observed_dependents(CONFIG, pytest_inputs(root), root=tmp_path) == {"core": ("engine",)}


def test_no_scope_becomes_its_own_dependent(tmp_path: Path) -> None:
    """`engine` imports `library`, so `library` is upstream of the file `tests/library/` borrows."""
    root = _tree(
        tmp_path,
        {
            "apps/engine_core/thing.py": "from apps.core import base\nfrom apps.library import shelf\n",
            "tests/library/conftest.py": "from tests.engine_core.conftest import Thing  # noqa: F401\n",
        },
    )
    assert observed_dependents(CONFIG, pytest_inputs(root), root=tmp_path) == {
        "core": ("engine", "library"),
        "engine": ("library",),
        "library": ("engine",),
    }
