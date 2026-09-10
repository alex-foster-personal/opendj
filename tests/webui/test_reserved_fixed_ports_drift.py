"""RESERVED_FIXED_PORTS must not silently drift from the suites it protects.

apps/webui/port_config.py.RESERVED_FIXED_PORTS (issue #1613) hand-lists every
fixed, compile-time port a Playwright/Vite/WDIO e2e or desktop suite binds
directly, so the dynamic worktree-port allocator never hands one out by
coincidence. Each suite still carries its OWN pinned-port literal (that is
what actually gates it at runtime); this only checks that the two lists agree.

If a suite's own literal changes and this set is not updated with it, the
dynamic allocator silently starts treating that port as free again -- exactly
the class of bug #1613 was filed against, reintroduced one line at a time.

Regression line: if any suite's fixed port here is not in RESERVED_FIXED_PORTS
then broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.webui.port_config import RESERVED_FIXED_PORTS

REPO = Path(__file__).resolve().parents[2]
E2E_DIR = REPO / "apps/webui/frontend/tests/e2e"
DESKTOP_DIR = REPO / "apps/desktop"

#: (relative path, exact literal expected in the source, the port it pins).
#: The literal is the smallest substring that (a) actually appears verbatim in
#: the file today and (b) would break if the port value changed, so a future
#: renumbering cannot pass this test without being noticed here too.
KNOWN_FIXED_PORTS: tuple[tuple[str, str, int], ...] = (
    ("apps/desktop/wdio.conf.ts", "const EMBEDDED_WEBDRIVER_PORT = 4455;", 4455),
    ("apps/desktop/mcp/smoke.ts", "const WEBDRIVER_PORT = 4456;", 4456),
    ("apps/webui/frontend/tests/e2e/vite.play-analytics.config.ts", "port: 5214,", 5214),
    (
        "apps/webui/frontend/tests/e2e/playwright.desktop-setup.config.ts",
        "export const SETUP_PAGE_PORT = 5216;",
        5216,
    ),
    (
        "apps/webui/frontend/tests/e2e/vite.performance.config.ts",
        "const DEFAULT_FRONTEND_BASE = 'http://127.0.0.1:5273';",
        5273,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.stretch-artifact.config.ts",
        "export const STRETCH_ARTIFACT_PORT = 5311;",
        5311,
    ),
    (
        "apps/webui/frontend/tests/e2e/vite.hotcue-mapping-gate.config.ts",
        "export const HOTCUE_MAPPING_GATE_FRONTEND_PORT = 5320;",
        5320,
    ),
    (
        "apps/webui/frontend/tests/e2e/vite.comment-hotkey-gate.config.ts",
        "export const COMMENT_HOTKEY_GATE_FRONTEND_PORT = 5321;",
        5321,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.meter-artifact.config.ts",
        "export const METER_ARTIFACT_PORT = 5322;",
        5322,
    ),
    (
        "apps/webui/frontend/tests/e2e/vite.full-reload-gate.config.ts",
        "export const FULL_RELOAD_GATE_FRONTEND_PORT = 5323;",
        5323,
    ),
    (
        "apps/webui/frontend/tests/e2e/vite.autoplay-stall-gate.config.ts",
        "export const AUTOPLAY_STALL_GATE_FRONTEND_PORT = 5324;",
        5324,
    ),
    (
        "apps/webui/frontend/tests/e2e/vite.rekordbox-gate.config.ts",
        "export const REKORDBOX_GATE_E2E_PORT = 5399;",
        5399,
    ),
    (
        "apps/webui/frontend/tests/e2e/vite.performance.config.ts",
        "const DEFAULT_API_BASE = 'http://127.0.0.1:8686';",
        8686,
    ),
    (
        "apps/webui/frontend/tests/e2e/stems-e2e-endpoints.ts",
        "const DEFAULT_BACKEND_PORT = 8688;",
        8688,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.webkit-deckload.config.ts",
        "export const WEBKIT_DECKLOAD_PORT = 8690;",
        8690,
    ),
    ("apps/desktop/wdio.conf.ts", "const ENGINE_PORT = 8691;", 8691),
    (
        "apps/webui/frontend/tests/e2e/playwright.boot-burst.config.ts",
        "process.env.BOOT_BURST_PORT ?? 8692",
        8692,
    ),
    (
        "apps/webui/frontend/tests/e2e/vite.hotcue-mapping-gate.config.ts",
        "export const HOTCUE_MAPPING_GATE_API_PORT = 8695;",
        8695,
    ),
    (
        "apps/webui/frontend/tests/e2e/vite.comment-hotkey-gate.config.ts",
        "export const COMMENT_HOTKEY_GATE_API_PORT = 8696;",
        8696,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.preflight-gate.config.ts",
        "export const PREFLIGHT_GATE_BROKEN_API_PORT = 8697;",
        8697,
    ),
    ("apps/desktop/mcp/smoke.ts", "const ENGINE_PORT = 8698;", 8698),
    (
        "apps/webui/frontend/tests/e2e/vite.autoplay-stall-gate.config.ts",
        "export const AUTOPLAY_STALL_GATE_API_PORT = 8699;",
        8699,
    ),
    (
        "apps/webui/frontend/tests/e2e/stems-e2e-endpoints.ts",
        "const DEFAULT_FRONTEND_PORT = 9408;",
        9408,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.play-analytics.config.ts",
        "--port 9414",
        9414,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.desktop-setup.config.ts",
        "export const DEAD_ENGINE_PORT = 9473;",
        9473,
    ),
)


@pytest.mark.parametrize(
    "relative_path,literal,port",
    KNOWN_FIXED_PORTS,
    ids=[f"{path}:{port}" for path, _literal, port in KNOWN_FIXED_PORTS],
)
def test_known_fixed_port_literal_still_matches_reserved_set(
    relative_path: str, literal: str, port: int
) -> None:
    """if a suite's own pinned-port literal no longer reads as recorded here
    then broken -- this table and RESERVED_FIXED_PORTS would be silently
    describing a port the suite no longer binds"""
    source = (REPO / relative_path).read_text(encoding="utf-8")

    assert literal in source, (
        f"{relative_path} no longer contains {literal!r}; re-derive its fixed port "
        "and update both this table and RESERVED_FIXED_PORTS"
    )
    assert port in RESERVED_FIXED_PORTS, (
        f"{relative_path} pins port {port} but RESERVED_FIXED_PORTS does not exclude it, "
        "so the dynamic allocator could still hand it out (issue #1613)"
    )


def test_every_known_fixed_port_is_covered_by_reserved_fixed_ports() -> None:
    """CONTROL: an empty table would satisfy the parametrized test above and
    guard nothing, so also check that the known set has real members and
    that it fully covers RESERVED_FIXED_PORTS -- either side growing without
    the other is the drift this file exists to catch."""
    known_ports = {port for _path, _literal, port in KNOWN_FIXED_PORTS}
    assert len(known_ports) >= 20, "the known-fixed-port table looks truncated"
    assert known_ports == set(RESERVED_FIXED_PORTS), (
        f"known suite ports and RESERVED_FIXED_PORTS disagree: "
        f"known-only={sorted(known_ports - RESERVED_FIXED_PORTS)}, "
        f"reserved-only={sorted(RESERVED_FIXED_PORTS - known_ports)}"
    )
