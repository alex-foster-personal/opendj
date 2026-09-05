"""DEVLOOP-01 acceptance coverage for the documented desktop attach loop.

Requirements:

- ✔︎ ✅ 🎯 ``just dev-attach`` launches the debug shell against the claimed engine.
- ✔︎ ✅ 🎯 The desktop runbook separates UI, engine, and shell feedback loops.
- ✔︎ ✅ 🎯 UI-only WebKit truth needs neither Cargo nor a DMG.

Acceptance tests:

- [if] the attach recipe stops injecting the claimed engine origin [then ⛔️]
  the debug shell launches against an engine it started itself.
- [if] the UI-only Safari path mentions Cargo or a DMG [then ⛔️] the
  documented inner loop regresses to release-artifact latency.
- [if] any independent loop loses its executable command [then ⛔️] a
  new contributor cannot reproduce the documented topology.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("DEVLOOP-01")

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_README = REPO_ROOT / "apps" / "desktop" / "README.md"
JUSTFILE = REPO_ROOT / "justfile"


def _dev_attach_recipe() -> str:
    lines = JUSTFILE.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("dev-attach "))
    body: list[str] = [lines[start]]
    for line in lines[start + 1 :]:
        if line and not line.startswith((" ", "\t")):
            break
        body.append(line)
    return "\n".join(body)


def test_dev_attach_launches_debug_shell_against_claimed_engine() -> None:
    """If dev-attach does not wire the two seams together then broken."""
    recipe = _dev_attach_recipe()

    assert "apps.webui.port_config show --json" in recipe
    assert 'OPENDJ_ENGINE_ORIGIN="$engine_origin"' in recipe
    assert '"$engine_origin/"' in recipe
    assert "%{content_type}" in recipe
    assert "text/html" in recipe
    assert recipe.count("--connect-timeout") == 2
    assert recipe.count("--max-time") == 2
    assert "--message-format=json-render-diagnostics" in recipe
    assert '["executable"]' in recipe
    assert "cargo build --manifest-path apps/desktop/src-tauri/Cargo.toml" in recipe
    assert "dev-attach requires Cargo on PATH" in recipe
    assert 'exec "$desktop_binary"' in recipe
    assert "apps/desktop/src-tauri/target/debug/opendj-desktop" not in recipe
    assert '"$cargo_target_directory/debug/opendj-desktop"' not in recipe
    assert "TAURI_WEBDRIVER_PORT" in recipe
    assert "cargo tauri build" not in recipe
    assert "just dmg" not in recipe


def test_ui_only_webkit_truth_requires_no_cargo_build_or_dmg() -> None:
    """If the Safari UI loop invokes Cargo or a DMG then broken."""
    readme = DESKTOP_README.read_text(encoding="utf-8")
    start = readme.index("### WebKit-truthful UI loop")
    end = readme.index("### Engine loop", start)
    webkit_loop = readme[start:end]

    assert "pnpm build --watch" in webkit_loop
    assert "Safari" in webkit_loop
    assert "just webui-webkit-watch" in webkit_loop
    assert "cargo" not in webkit_loop.lower()
    assert "dmg" not in webkit_loop.lower()


def test_readme_documents_three_independent_loops() -> None:
    """If a loop has no command a contributor cannot run it independently."""
    readme = DESKTOP_README.read_text(encoding="utf-8")

    assert "## Development loops: attach without a release artifact" in readme
    assert "### Fast UI loop" in readme
    assert "pnpm exec vite dev --host 127.0.0.1" in readme
    assert "### WebKit-truthful UI loop" in readme
    assert "### Engine loop" in readme
    assert "just webui-backend" in readme
    assert "### Shell loop" in readme
    assert "just dev-attach" in readme
    assert "webview MCP" in readme
    assert "The DMG is a release artifact only." in readme
