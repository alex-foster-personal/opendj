"""Root Playwright suite must be executable and enforced.

Issue #814 found that the default Playwright config selected browser specs but
started only Vite. API requests therefore failed at the proxy, and CI never
invoked the suite. These checks keep the runnable root command and the E2E
workflow tied together.
"""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ROOT_CONFIG = REPO / "apps/webui/frontend/playwright.config.ts"
PERFORMANCE_CONFIG = REPO / "apps/webui/frontend/tests/e2e/playwright.performance.config.ts"
SMOKE_SPEC = REPO / "apps/webui/frontend/tests/e2e/smoke.spec.ts"
E2E_WORKFLOW = REPO / ".github/workflows/e2e.yml"
WORKFLOWS_DIR = REPO / ".github/workflows"
PACKAGE = REPO / "apps/webui/frontend/package.json"
PERFORMANCE_CONFIG_MARKER = "playwright.performance.config.ts"


def test_root_playwright_config_starts_the_engine_before_vite() -> None:
    """if a root spec requests /api then its proxy has a real engine to reach"""
    source = ROOT_CONFIG.read_text()

    assert "'apps.engine_core', 'serve'" in source
    assert "rmSync(FIXTURE_DATA_DIR, { recursive: true, force: true })" in source
    assert "TEST_WORKER_INDEX" in source
    assert "webServer: [" in source
    assert "api/v1/health" in source
    assert "root-playwright-data" in source
    assert "fullyParallel: false" in source
    assert "workers: 1" in source


def test_e2e_workflow_executes_the_root_playwright_command() -> None:
    """if the default suite is removed from CI then its regressions are invisible"""
    workflow = yaml.safe_load(E2E_WORKFLOW.read_text())
    commands = [
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
    ]

    assert any("pnpm test:e2e" in command for command in commands)


def test_performance_playwright_config_is_referenced_by_ci() -> None:
    """if the performance suite is not invoked by any workflow then its failures are invisible"""
    workflow_texts = [path.read_text(encoding="utf-8") for path in WORKFLOWS_DIR.glob("*.yml")]
    assert any(
        PERFORMANCE_CONFIG_MARKER in text or "test:e2e:performance" in text
        for text in workflow_texts
    ), (
        f"{PERFORMANCE_CONFIG_MARKER} must appear in a workflow step or "
        "package script wired into CI (issue #2090 / CUEOUT-06)"
    )


def test_e2e_workflow_runs_the_headphone_device_probe_under_performance_config() -> None:
    """if the headphone probe is not exercised in CI then aria-label regressions time out unseen"""
    workflow = yaml.safe_load(E2E_WORKFLOW.read_text())
    commands = [
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
    ]
    assert any(
        PERFORMANCE_CONFIG_MARKER in command
        and "performance-headphone-device-probe.spec.ts" in command
        for command in commands
    )


def test_root_playwright_command_claims_its_isolated_port_pair() -> None:
    """if a clean checkout runs test:e2e then port validation has a reservation"""
    script = PACKAGE.read_text()

    assert "test:e2e" in script
    assert "apps.webui.port_config claim" in script
    assert "uv sync --extra dev --extra tags --extra analysis" in script


def test_fixture_reset_is_bounded_to_the_repository_fixtures_directory() -> None:
    """if the reset target is not the fixtures dir then the config refuses to run"""
    source = ROOT_CONFIG.read_text()

    # The recursive delete is in-process, so the only thing standing between it
    # and an unrelated directory is this bound. Deleting the bound left every
    # other check in this file green, which is why it is asserted here.
    assert "FIXTURE_PARENT_DIR" in source
    assert "dirname(FIXTURE_DATA_DIR) !== FIXTURE_PARENT_DIR" in source
    assert "Refusing to reset fixture outside" in source
    # And the path never reaches a shell as a bare word.
    assert "rm -rf" not in source
    assert "shellArgument" in source


def test_stem_decode_bench_spec_is_ignored_by_the_root_suite() -> None:
    """if the bench spec is reachable from pnpm test:e2e then the root suite
    loads playwright.stem-decode-bench.config.ts, which throws without a
    production build (CI e2e gate on PR #2028)."""
    source = ROOT_CONFIG.read_text()
    assert "'**/stem-decode-bench.spec.ts'" in source


def test_playlist_switch_latency_spec_is_ignored_by_the_root_suite() -> None:
    """if the PERF-UI-05 bench is reachable from pnpm test:e2e then the root
    suite loads playwright.playlist-switch-latency.config.ts, which throws
    without a production build."""
    source = ROOT_CONFIG.read_text()
    assert "'**/library-playlist-switch-latency.spec.ts'" in source


def test_every_playlist_switch_config_spec_is_ignored_by_the_root_suite() -> None:
    """if any spec the playlist-switch-latency config selects is reachable from
    pnpm test:e2e then it runs against the root suite's fixture, which has no
    "Perf 1k" playlist (LIBM-134's fill spec failed that way on PR #4582)."""
    config = (
        REPO / "apps/webui/frontend/tests/e2e/playwright.playlist-switch-latency.config.ts"
    ).read_text()
    match = re.search(r"testMatch: \[([^\]]*)\]", config)
    assert match, "playlist-switch-latency config has no testMatch list"
    specs = re.findall(r"'([^']+\.spec\.ts)'", match.group(1))
    assert "library-playlist-fill-pages.spec.ts" in specs, specs
    source = ROOT_CONFIG.read_text()
    missing = [spec for spec in specs if f"'**/{spec}'" not in source]
    assert not missing, f"root playwright.config.ts does not ignore {missing}"


def test_smoke_console_exemption_stays_specific_to_the_update_failure() -> None:
    """if another resource breaks then a failing update check cannot hide it"""
    spec = SMOKE_SPEC.read_text()

    # `Failed to load resource` is emitted verbatim for EVERY failed request,
    # so a substring exemption swallowed unrelated breakage whenever the update
    # check happened to be down. Relaxing it back left this file green.
    assert "error.includes('Failed to load resource')" not in spec
    assert "the server responded with a status of 502 (Bad Gateway)'" in spec
    # The exemption is only reached after the response is confirmed to be the
    # update check's own documented failure payload...
    assert "update_check_failed" in spec
    # ...and a request that fails before any response still fails the test.
    assert "requestfailed" in spec
    assert "expect(failedRequests).toEqual([])" in spec
