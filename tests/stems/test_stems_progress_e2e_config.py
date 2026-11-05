"""Source-shape pin: stems-progress e2e uses direct transport on the engine env."""

from __future__ import annotations

from pathlib import Path

_CONFIG = (
    Path(__file__).resolve().parents[2] / "apps/webui/frontend/tests/e2e/playwright.stems.config.ts"
)


def test_playwright_stems_config_sets_direct_transport_on_engine_env() -> None:
    text = _CONFIG.read_text(encoding="utf-8")
    assert "MDT_STEMS_TRANSPORT" in text
    engine_region = text.split("const engineEnv", 1)[1].split("export default defineConfig", 1)[0]
    assert "'direct'" in engine_region
    assert "MDT_STEMS_SEPARATOR" in engine_region
    assert "modal_separator_double" in engine_region
