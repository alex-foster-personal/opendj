"""DEVLOOP-03 production-build watcher regression coverage.

- [if] a change is saved [then] the engine serves the rebuilt production bundle,
  transformed exactly as a shipped build would be ⛔️
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("DEVLOOP-03")

REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE = REPO_ROOT / "justfile"


def _recipe(name: str) -> str:
    lines = JUSTFILE.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(f"{name}:"))
    body = [lines[start]]
    for line in lines[start + 1 :]:
        if line and not line.startswith((" ", "\t")):
            break
        body.append(line)
    return "\n".join(body)


def test_webkit_watch_loop_serves_vite_production_build_after_each_save() -> None:
    """A source save must rebuild the artifact that the engine serves."""
    recipe = _recipe("webui-webkit-watch")

    assert "pnpm build --watch" in recipe, (
        "if the watch loop uses vite dev then untransformed packages can hide a shipped-build "
        "failure - broken"
    )
    assert "MDT_FRONTEND_BUILD_DIR" in recipe
    assert "apps/webui/frontend/build" in recipe
    assert "just webui-backend" in recipe
    assert 'grep -q "built in"' in recipe, (
        "if the engine starts before the watcher's initial production build then it can mount a "
        "stale bundle - broken"
    )
