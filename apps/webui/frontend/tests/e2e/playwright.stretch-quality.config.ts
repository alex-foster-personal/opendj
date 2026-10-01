/**
 * Stretch-quality render config.
 *
 * No `webServer`, no port, no data dir: the vendored signalsmith module and
 * the fixture bodies are served to a synthetic origin by request interception,
 * so this config boots nothing. Fixture EXTRACTION talks to the lane daemon,
 * and that is a separate step which has already run.
 *
 * Requirements:
 *
 * - ✔︎ One worker, serial: every render is CPU-bound and the metrics compare
 *   arms measured on the same machine under the same load.
 * - ✔︎ Zero retries: a flaky render is a determinism failure, and re-running it
 *   until it passes is exactly the thing this harness exists to catch.
 *
 * Acceptance tests:
 *
 * - [if] a render is non-deterministic [then ⛔️] a retry hides it.
 * - [if] the config starts a dev server [then ⛔️] the run needs a free port.
 */
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'stretch-quality.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 3 * 60 * 60_000,
	globalTimeout: 4 * 60 * 60_000,
	reporter: [['list']],
	use: {
		trace: 'off',
		screenshot: 'off',
		video: 'off'
	},
	projects: [
		{
			name: 'stretch-quality-chromium',
			use: { ...devices['Desktop Chrome'] }
		}
	]
});
