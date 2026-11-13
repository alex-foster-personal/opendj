"""genre_infer never imports apps.analysis, so the analysis <-> genre_infer cycle stays closed (#13074).

[if] the package-cycle scanner runs [then] analysis <-> genre_infer is not a pair, [else stop].

-Claude
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from apps.analysis import queue_store, queue_user_lanes
from apps.shared.state import analysis_queue_ids

pytestmark = pytest.mark.requirement("QUALITY-13074")

GENRE_DIR = Path(__file__).resolve().parents[2] / "apps" / "genre_infer"


def _analysis_imports(path: Path) -> list[str]:
    """Return every module-level-or-nested static import of apps.analysis in one file."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "apps.analysis" or node.module.startswith("apps.analysis."):
                found.append(f"{path.name}:{node.lineno} from {node.module}")
            elif node.module == "apps" and any(a.name == "analysis" for a in node.names):
                found.append(f"{path.name}:{node.lineno} from apps import analysis")
        elif isinstance(node, ast.Import):
            found.extend(
                f"{path.name}:{node.lineno} import {alias.name}"
                for alias in node.names
                if alias.name == "apps.analysis" or alias.name.startswith("apps.analysis.")
            )
    return found


def test_genre_infer_has_no_static_analysis_import() -> None:
    """[if] genre inference imports its shared model [then] no apps/genre_infer module statically imports apps.analysis, [else stop]."""
    hits = [hit for path in sorted(GENRE_DIR.glob("*.py")) for hit in _analysis_imports(path)]
    assert hits == [], f"genre_infer must read shared queue literals from apps.shared: {hits}"


def test_analysis_reexports_the_shared_queue_literals() -> None:
    """[if] analysis reads its queue literals [then] they are the same objects genre_infer reads from apps.shared, [else stop]."""
    assert queue_store.ITEM_PENDING == analysis_queue_ids.ITEM_PENDING == "pending"
    assert queue_store.ITEM_RUNNING == analysis_queue_ids.ITEM_RUNNING == "running"
    assert queue_user_lanes.USER_BATCH_IDS is analysis_queue_ids.USER_BATCH_IDS
    assert analysis_queue_ids.USER_BATCH_IDS["genre"] == "ub_genre"
