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
then broken, and if a NEWLY ADDED configuration's pinned port is not
discovered (so it could be added without redding anything) then broken too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.webui.port_config import RESERVED_FIXED_PORTS

REPO = Path(__file__).resolve().parents[2]
E2E_DIR = REPO / "apps/webui/frontend/tests/e2e"
DESKTOP_DIR = REPO / "apps/desktop"

#: A loopback port pinned as a literal. Same shape tests/scripts/
#: test_ci_e2e_fixed_port_steps_are_locked.py uses to find a suite's own port.
FIXED_PORT_LITERAL = re.compile(
    r"(?:PORT[A-Za-z_]*\s*=\s*|PORT[A-Za-z_]*\s*\?\?\s*|127\.0\.0\.1:|localhost:)(\d{4,5})\b"
)


def discovered_fixed_ports(roots: tuple[Path, ...] = (E2E_DIR, DESKTOP_DIR)) -> dict[int, set[str]]:
    """Every pinned-port literal the candidate configuration trees declare.

    Derived from the files themselves rather than from ``KNOWN_FIXED_PORTS``,
    which is the list this module exists to police: a suite that adds a fixed
    port without editing that table would otherwise generate no test at all
    and pass by omission.
    """
    discovered: dict[int, set[str]] = {}
    for root in roots:
        for path in sorted(root.rglob("*.ts")):
            if "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for literal in FIXED_PORT_LITERAL.findall(text):
                discovered.setdefault(int(literal), set()).add(str(path.relative_to(root)))
    return discovered


def test_every_declared_fixed_port_is_discovered_and_excluded() -> None:
    """if a configuration gains a pinned port without RESERVED_FIXED_PORTS
    gaining it -- or stops declaring one the set still excludes -- then broken
    (issue #1613)"""
    discovered = discovered_fixed_ports()
    assert discovered, "the discovery probe found no fixed port at all; the probe is broken"

    unguarded = {
        port: sorted(paths)
        for port, paths in discovered.items()
        if port not in RESERVED_FIXED_PORTS
    }
    assert not unguarded, (
        "these configurations pin a port RESERVED_FIXED_PORTS does not exclude, so the "
        f"dynamic allocator could still hand it out (issue #1613): {unguarded}"
    )

    undeclared = sorted(set(RESERVED_FIXED_PORTS) - set(discovered))
    assert not undeclared, (
        f"RESERVED_FIXED_PORTS excludes {undeclared}, which no configuration declares any "
        "more; the reservation is stale and should be removed with the port"
    )


def test_the_discovery_probe_finds_a_port_in_a_new_configuration(tmp_path: Path) -> None:
    """CONTROL: the probe above passes trivially if it walks nothing, so point
    it at a tree shaped like a NEWLY ADDED configuration and require it to
    find the port -- a guard against a config should red."""
    (tmp_path / "vite.brand-new-gate.config.ts").write_text(
        "export const BRAND_NEW_GATE_PORT = 5398;\n", encoding="utf-8"
    )
    assert discovered_fixed_ports((tmp_path,)) == {5398: {"vite.brand-new-gate.config.ts"}}

#: (relative path, exact literal expected in the source, the port it pins).
#: The literal is the smallest substring that (a) actually appears verbatim in
#: the file today and (b) would break if the port value changed, so a future
#: renumbering cannot pass this test without being noticed here too.
KNOWN_FIXED_PORTS: tuple[tuple[str, str, int], ...] = (
    ("apps/desktop/wdio.conf.ts", "const EMBEDDED_WEBDRIVER_PORT = 4455;", 4455),
    ("apps/desktop/mcp/smoke.ts", "const WEBDRIVER_PORT = 4456;", 4456),
    ("apps/webui/frontend/tests/e2e/vite.play-analytics.config.ts", "port: 5214,", 5214),
    ("apps/webui/frontend/tests/e2e/vite.library-wheel.config.ts", "port: 5228,", 5228),
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
        "apps/webui/frontend/tests/e2e/library-jobs-e2e-endpoints.ts",
        "export const LIBRARY_JOBS_E2E_FRONTEND_PORT = 5277;",
        5277,
    ),
    (
        "apps/webui/frontend/tests/e2e/vocals-demucs-overlay-endpoints.ts",
        "export const VOCALS_DEMUCS_OVERLAY_FRONTEND_PORT = 5278;",
        5278,
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
        "apps/webui/frontend/tests/e2e/vite.lyrics-words.config.ts",
        "export const LYRICS_WORDS_FRONTEND_PORT = 5328;",
        5328,
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
        "apps/webui/frontend/tests/e2e/vite.autoplay-error-hunt.config.ts",
        "export const AUTOPLAY_HUNT_FRONTEND_PORT = 5326;",
        5326,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.cloudsync-ui.config.ts",
        "export const CLOUDSYNC_UI_HUB_PORT = 8711;",
        8711,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.cloudsync-ui.config.ts",
        "export const CLOUDSYNC_UI_SPOKE_PORT = 8712;",
        8712,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.cloudsync-ui.config.ts",
        "export const CLOUDSYNC_UI_FRONTEND_PORT = 5331;",
        5331,
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
        "apps/webui/frontend/tests/e2e/playwright.stem-decode-bench.config.ts",
        "process.env.STEM_DECODE_BENCH_PORT ?? 8700",
        8700,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.playlist-switch-latency.config.ts",
        "process.env.PLAYLIST_SWITCH_BENCH_PORT ?? 8713",
        8713,
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
        "apps/webui/frontend/tests/e2e/vite.autoplay-error-hunt.config.ts",
        "export const AUTOPLAY_HUNT_API_PORT = 8703;",
        8703,
    ),
    (
        "apps/webui/frontend/tests/e2e/library-jobs-e2e-endpoints.ts",
        "export const LIBRARY_JOBS_E2E_BACKEND_PORT = 8704;",
        8704,
    ),
    (
        "apps/webui/frontend/tests/e2e/vocals-demucs-overlay-endpoints.ts",
        "export const VOCALS_DEMUCS_OVERLAY_BACKEND_PORT = 8705;",
        8705,
    ),
    (
        "apps/webui/frontend/tests/e2e/vite.lyrics-words.config.ts",
        "export const LYRICS_WORDS_API_PORT = 8706;",
        8706,
    ),
    (
        "apps/webui/frontend/tests/e2e/stems-e2e-endpoints.ts",
        "const DEFAULT_FRONTEND_PORT = 9408;",
        9408,
    ),
    (
        "apps/webui/frontend/tests/e2e/midi-maps-e2e-endpoints.ts",
        "const DEFAULT_BACKEND_PORT = 8690;",
        8690,
    ),
    (
        "apps/webui/frontend/tests/e2e/midi-maps-e2e-endpoints.ts",
        "const DEFAULT_FRONTEND_PORT = 9410;",
        9410,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.play-analytics.config.ts",
        "--port 9414",
        9414,
    ),
    (
        "apps/webui/frontend/tests/e2e/playwright.library-wheel.config.ts",
        "--port 9428",
        9428,
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
