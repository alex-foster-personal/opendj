/**
 * Synth-signals hash spec (issue #10402).
 *
 * No webServer: compares Chromium SHA-256 of DEFAULT_RECIPES to node and the manifest.
 *
 * Acceptance tests:
 *
 * - [if] the config starts a dev server [then stop] the run needs a free port.
 * - [if] Chromium hashes differ from node [then] the generator is not portable.
 */
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'synth-signals-hash.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	reporter: [['list']],
	use: { trace: 'off', screenshot: 'off', video: 'off' },
	projects: [{ name: 'synth-signals-chromium', use: { ...devices['Desktop Chrome'] } }]
});
