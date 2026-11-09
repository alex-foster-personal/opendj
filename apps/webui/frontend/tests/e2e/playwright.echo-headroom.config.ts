/**
 * FXECHO-10612 headroom spec: no webServer, production kernel in OfflineAudioContext.
 */
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'echo-headroom-real-audio.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 120_000,
	reporter: [['list']],
	use: { trace: 'off', screenshot: 'off', video: 'off' },
	projects: [{ name: 'echo-headroom-chromium', use: { ...devices['Desktop Chrome'] } }]
});
