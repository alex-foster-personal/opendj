/**
 * REFRESH-01 full-reload gate, end to end (PR #890 review r3920753724).
 *
 * `reload-countdown-browser.spec.ts`'s "dev full-reload receiver" test only
 * proves the receiver module loaded as a real (not inline-body) request and
 * that `import.meta.hot` was truthy there - it never causes Vite to emit a
 * REAL `full-reload`, so it stays green even if `holdFullReloadPlugin()` is
 * removed, `server.hot.send` interception stops matching, or the custom
 * event never reaches the listener. This suite closes that gap: it edits a
 * real source file on disk, waits for Vite's own HMR machinery to decide the
 * update needs a full reload (see full-reload-gate.spec.ts's docstring for
 * why that file is the one edited), and asserts the countdown overlay holds
 * it before the ensuing navigation - the exact REFRESH-01 acceptance line.
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.full-reload-gate.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { FULL_RELOAD_GATE_FRONTEND_PORT } from './vite.full-reload-gate.config';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${FULL_RELOAD_GATE_FRONTEND_PORT}`;

export default defineConfig({
	testDir: '.',
	testMatch: 'full-reload-gate.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 45_000,
	globalTimeout: 120_000,
	expect: { timeout: 10_000 },
	reporter: [['list']],
	webServer: {
		command: guardedWebServerCommand('full-reload-gate-vite', `pnpm exec vite --config tests/e2e/vite.full-reload-gate.config.ts`),
		cwd: FRONTEND_ROOT,
		url: FRONTEND_ORIGIN,
		reuseExistingServer: false,
		timeout: 30_000
	},
	use: {
		baseURL: FRONTEND_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'full-reload-gate-chromium', use: { ...devices['Desktop Chrome'] } }]
});
