"""Pytest plugin: record requirement markers for one disposition-validation target."""

from __future__ import annotations

import json
import os
from pathlib import Path


def pytest_configure(config) -> None:  # type: ignore[no-untyped-def]
    config.addinivalue_line("markers", "requirement(id): requirement id marker")


def pytest_collection_modifyitems(config, items) -> None:  # type: ignore[no-untyped-def]
    out = Path(os.environ["REQHYG03_MARKS_OUT"])
    target = os.environ["REQHYG03_TARGET"]
    selected = [i for i in items if i.nodeid == target or i.nodeid.startswith(target + "[")]
    payload = {"found": False, "ids": [], "has_intent": False}
    if selected:
        item = selected[0]
        payload["found"] = True
        payload["ids"] = [str(m.args[0]) for m in item.iter_markers("requirement") if m.args]
        doc = getattr(item.obj, "__doc__", None) or ""
        payload["has_intent"] = "[if]" in doc and "[then]" in doc and "[else stop]" in doc
    out.write_text(json.dumps(payload), encoding="utf-8")
