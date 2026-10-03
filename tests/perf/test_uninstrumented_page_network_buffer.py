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

from tests.perf.playwright_chromium import chromium_unavailable_reason

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


def _node() -> str:
    node = shutil.which("node")
    if node is None or not (_FRONTEND / "node_modules" / "@playwright" / "test").is_dir():
        raise RuntimeError(
            "network_buffer_probe needs node and the frontend's node_modules "
            "(pnpm install in apps/webui/frontend, then pnpm exec playwright install chromium)"
        )
    return node


def _probe(page_kind: str) -> dict[str, float]:
    completed = subprocess.run(
        [_node(), str(_PROBE), "--page", page_kind],
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
    """[if] Playwright's own page fetches 48 MB [then] the renderer keeps most of it, so the probe can see buffering, [else stop]."""
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


_PREDICATE_THROW_SCRIPT = """
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const { openUninstrumentedPage } = await import(pathToFileURL(process.argv[1]).href);
const mod = await import(pathToFileURL(createRequire(path.join(process.cwd(), 'package.json')).resolve('@playwright/test')).href);
const browser = await (mod.default ?? mod).chromium.launch({ headless: true });
const page = await openUninstrumentedPage(browser);
const started = Date.now();
let message = 'did not reject';
try {
  await page.waitForFunction(() => { throw new Error('AudioContext was not allowed to start'); }, undefined, { timeout: 20000 });
} catch (error) {
  message = String(error.message);
}
console.log(JSON.stringify({ elapsed_ms: Date.now() - started, message }));
await browser.close();
"""


@_requires_reference_mac
@pytest.mark.requirement("PERFMODE-15")
def test_a_throwing_predicate_fails_at_once_instead_of_polling_to_timeout() -> None:
    """[if] a waitForFunction predicate throws an AudioContext error [then] it rejects with that error at once, [else stop]."""
    completed = subprocess.run(
        [
            _node(),
            "--input-type=module",
            "-e",
            _PREDICATE_THROW_SCRIPT,
            str(_REPO / "scripts" / "perf" / "uninstrumented-page.mjs"),
        ],
        cwd=_FRONTEND,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    assert "AudioContext was not allowed to start" in outcome["message"], outcome
    assert outcome["elapsed_ms"] < 5000, outcome


# ----- a target that goes away mid-command (Codex P1 r4171125805, PR #4888) ---

_DETACH_SCRIPT = """
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const [pageModule, mode] = process.argv.slice(1);
const { openUninstrumentedPage } = await import(pathToFileURL(pageModule).href);
const mod = await import(pathToFileURL(createRequire(path.join(process.cwd(), 'package.json')).resolve('@playwright/test')).href);
const browser = await (mod.default ?? mod).chromium.launch({ headless: true });
const page = await openUninstrumentedPage(browser);
const outcome = page.evaluate(() => new Promise(() => {})).then(
  () => ({ settled: true, error: null }),
  (error) => ({ settled: true, error: String(error.message) })
);
await new Promise((resolve) => setTimeout(resolve, 500));
if (mode === 'crash') page.send('Page.crash').catch(() => undefined);
else await page.close();
const deadline = new Promise((resolve) => setTimeout(() => resolve({ settled: false, error: null }), 15000));
const result = await Promise.race([outcome, deadline]);
let later = null;
try { await Promise.race([page.send('Runtime.evaluate', { expression: '1' }), deadline]); }
catch (error) { later = String(error.message); }
console.log(JSON.stringify({ ...result, later_error: later }));
await browser.close();
process.exit(0);
"""


@pytest.mark.requirement("PERFMODE-15")
@pytest.mark.parametrize(("mode", "reason"), [("crash", "renderer crashed"), ("close", "target detached")])
def test_an_in_flight_command_rejects_when_its_target_goes_away(mode: str, reason: str) -> None:
    """[if] the page's renderer crashes or its target closes mid-evaluate [then] that evaluate and later sends reject, never hang, [else stop].

    Real headless Chromium, real crash (`Page.crash`) and real close. Before
    the fix both cases left the evaluate pending past the 15 s deadline
    (measured on nucbox, Sat 3 Oct 2026), so `settled` is the discriminator.
    """
    unavailable = chromium_unavailable_reason()
    if unavailable is not None:
        pytest.skip(unavailable)
    completed = subprocess.run(
        [
            _node(),
            "--input-type=module",
            "-e",
            _DETACH_SCRIPT,
            str(_REPO / "scripts" / "perf" / "uninstrumented-page.mjs"),
            mode,
        ],
        cwd=_FRONTEND,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    assert outcome["settled"] is True, outcome
    assert reason in outcome["error"], outcome
    assert outcome["later_error"] is not None and reason in outcome["later_error"], outcome
