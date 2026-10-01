"""The feedback route module keeps headroom under the 600-line file-size limit.

`routes/feedback.py` reached 608 lines on the loop branch once placement
fields were added to the pin models. Its JSON storage helpers now live in
`routes/feedback_storage.py`; the route module re-exports them under the same
names, because six sibling modules import them from there.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from apps.webui.server.routes import feedback, feedback_storage

_ROUTE_MODULE = Path(feedback.__file__)
_MOVED = ("_load", "_load_general", "_save", "keep_unknown_fields", "write_atomic")


def test_the_route_module_has_real_headroom() -> None:
    """[if] feedback.py grows back toward the limit [then] the loop branch's additions push it over 600 again, [else stop]."""
    lines = len(_ROUTE_MODULE.read_text(encoding="utf-8").splitlines())
    assert 100 < lines < 560, lines


def test_the_moved_helpers_are_the_same_objects_under_their_old_names() -> None:
    """[if] a sibling's `from .feedback import _load` resolves to a copy [then] two modules hold different helpers, [else stop]."""
    for name in _MOVED:
        assert getattr(feedback, name) is getattr(feedback_storage, name), name


def test_storage_behavior_is_unchanged(tmp_path: Path) -> None:
    """[if] the move changed what is written or how a malformed store is refused [then] broken, [else stop]."""
    path = tmp_path / "feedback" / "comments.json"
    assert feedback_storage._load(path, "comments") == []
    feedback_storage._save(path, "comments", [{"id": "a", "text": "one"}])
    assert (
        path.read_text(encoding="utf-8")
        == '{\n  "comments": [\n    {\n      "id": "a",\n      "text": "one"\n    }\n  ]\n}\n'
    )
    assert feedback_storage._load(path, "comments") == [{"id": "a", "text": "one"}]
    assert [leftover.name for leftover in path.parent.iterdir()] == ["comments.json"]
    with pytest.raises(HTTPException) as refused:
        feedback_storage._load(path, "todos")
    assert refused.value.status_code == 500
    assert refused.value.detail["code"] == "FEEDBACK_STORE_MALFORMED"
    assert feedback_storage._load_general(tmp_path / "absent.json") == {
        "text": "",
        "updated_at": None,
        "build": None,
    }
    merged = feedback_storage.keep_unknown_fields(
        {"id": "a", "newer": 1, "nested": {"kept": True, "x": 1}}, {"id": "a", "nested": {"x": 2}}
    )
    assert merged == {"id": "a", "newer": 1, "nested": {"kept": True, "x": 2}}
