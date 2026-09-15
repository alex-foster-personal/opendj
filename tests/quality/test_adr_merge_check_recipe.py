"""The merge-lane ADR gate must stay wired in the justfile (issue #3076)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE = REPO_ROOT / "justfile"
RECIPE_NAME = "adr-merge-check"


def _recipe_body(name: str) -> str:
    text = JUSTFILE.read_text(encoding="utf-8")
    lines = text.splitlines()
    starts = [
        i
        for i, line in enumerate(lines)
        if re.match(rf"^{re.escape(name)}(\s+[^:]*)?:", line)
    ]
    if not starts:
        pytest.fail(f"justfile has no `{name}` recipe")
    body: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line and not line[0].isspace():
            break
        body.append(line)
    return "\n".join(body)


def test_adr_merge_check_recipe_exists() -> None:
    body = _recipe_body(RECIPE_NAME)
    assert re.search(r"scripts\.adr_check\s+--merge-base\s+origin/main", body)
