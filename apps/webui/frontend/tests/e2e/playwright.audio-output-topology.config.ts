/**
 * Output-topology render config.
 *
 * No `webServer`, no port, no data dir: the spec bundles the topology module
 * and renders it through a real OfflineAudioContext in a blank page.
 *
 * Requirements:
 *
 * - ✔︎ ✅ One worker, zero retries: the render is deterministic, so a retry
 *   could only hide a real difference.
 *
 * Acceptance tests:
 *
 * - [if] the config starts a dev server [then ⛔️] the run needs a free port.
 * - [if] a render differs between runs [then ⛔️] a retry hides it.
 */
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'audio-output-topology.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	reporter: [['list']],
	use: { trace: 'off', screenshot: 'off', video: 'off' },
	projects: [{ name: 'audio-output-topology-chromium', use: { ...devices['Desktop Chrome'] } }]
});
