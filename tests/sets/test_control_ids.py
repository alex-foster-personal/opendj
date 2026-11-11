"""Generated control-id TS stays in sync with the Python registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sets import control_ids as ids


@pytest.mark.requirement("SETREC-11805")
def test_generated_control_ids_ts_matches_python_registry():
    """[if] registry changes [then] generated TS is stale until regen, [else stop]."""
    rendered = ids.render_typescript()
    path = Path(ids._REPO_ROOT) / ids.GENERATED_TS_REL
    assert path.read_text(encoding="utf-8") == rendered
