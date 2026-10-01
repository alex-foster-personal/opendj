"""PERFMODE-15: the footprint capture's page must not buffer response bodies.

Playwright enables the Network domain on every page it creates, and the
renderer then keeps each response body for DevTools (up to about 200 MB). In
Trackify those bodies are whole audio files, so the 1 h leak capture measured
that buffer filling, not the app (Thu 1 Oct 2026, silver, 4f8c670f029).
`mode_ratio_browser.mjs` now drives `uninstrumented-page.mjs` pages instead.

Both tests run `scripts/perf/network_buffer_probe.mjs` against a REAL headless
Chromium: it fetches 12 x 4 MB no-store bodies, drops them, collects garbage,
and reads the renderer's Blink buffer partition from a memory-infra dump.
The Playwright page is the positive control: it proves the probe can see a
buffered body, so the uninstrumented page reading near zero is a measurement,
not a silent zero.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND = _REPO / "apps" / "webui" / "frontend"
_PROBE = _REPO / "scripts" / "perf" / "network_buffer_probe.mjs"

_requires_reference_mac = pytest.mark.skipif(
    sys.platform != "darwin",
    reason=(
        "capture_mode_ratios refuses non-macOS hosts, so its page is checked on "
        "macOS only, matching test_capture_mode_ratios_browser_pid.py"
    ),
)


def _probe(page_kind: str) -> dict[str, float]:
    node = shutil.which("node")
    if node is None or not (_FRONTEND / "node_modules" / "@playwright" / "test").is_dir():
        pytest.fail(
            "network_buffer_probe needs node and the frontend's node_modules "
            "(pnpm install in apps/webui/frontend, then pnpm exec playwright install chromium)"
        )
    completed = subprocess.run(
        [node, str(_PROBE), "--page", page_kind],
        cwd=_FRONTEND,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"network_buffer_probe --page {page_kind} could not measure: {completed.stderr[-2000:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


@_requires_reference_mac
@pytest.mark.requirement("PERFMODE-15")
def test_control_a_playwright_page_buffers_the_bodies_it_fetched() -> None:
    """[if] Playwright's own page fetches 48 MB [then] the renderer still holds most of it, [else the probe is blind]."""
    reading = _probe("playwright")
    assert reading["pa_buffer_mb"] >= 0.75 * reading["bodies_mb"], reading


@_requires_reference_mac
@pytest.mark.requirement("PERFMODE-15")
def test_the_capture_page_does_not_buffer_fetched_bodies() -> None:
    """[if] the capture's uninstrumented page fetches 48 MB [then] the renderer keeps under a quarter of it, [else stop]."""
    reading = _probe("uninstrumented")
    assert reading["pa_buffer_mb"] <= 0.25 * reading["bodies_mb"], reading


@pytest.mark.requirement("PERFMODE-15")
def test_mode_ratio_browser_opens_only_uninstrumented_pages() -> None:
    """[if] the capture helper opens a page [then] it is an uninstrumented one, never Playwright's newPage, [else stop]."""
    source = (_REPO / "scripts" / "perf" / "mode_ratio_browser.mjs").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("*", "/", "//")))
    assert "openUninstrumentedPage(" in code
    assert ".newPage(" not in code and ".newContext(" not in code
