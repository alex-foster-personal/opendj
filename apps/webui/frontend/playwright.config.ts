import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { claimAndCheckWebuiDevConfig, claimWebuiDevConfig } from './webui-port-config';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url));

/**
 * Playwright re-loads this config inside every worker process, and it does so
 * AFTER the runner's webServer has bound the frontend port. Re-running the
 * "is this port free" guard there asserts against the server this very run
 * just started, so the guard runs once, in the runner. A genuine collision
 * with somebody else's dev server is still caught, by the guard on the first
 * load and by `reuseExistingServer: false` below.
 */
const IS_TEST_WORKER = process.env.TEST_WORKER_INDEX !== undefined;
const ports = IS_TEST_WORKER
	? claimWebuiDevConfig(REPOSITORY_ROOT)
	: claimAndCheckWebuiDevConfig(REPOSITORY_ROOT, 'frontend');

export default defineConfig({
	testDir: './tests/e2e',
	// Real-library performance contracts have their own servers and config.
	// Run them explicitly with `pnpm test:e2e:performance`.
	testIgnore: '**/performance-*.spec.ts',
	timeout: 30_000,
	webServer: {
		// --host is not decoration. Vite's default binds `localhost`, which on
		// this machine resolves to ::1 ONLY, while baseURL below dials
		// 127.0.0.1 -- every test in every suite then died on
		// ERR_CONNECTION_REFUSED while `port` reported the server up. Bind the
		// exact address the tests connect to, so the two can never disagree.
		command: `pnpm dev --host 127.0.0.1`,
		port: ports.frontendPort,
		reuseExistingServer: false,
	},
	use: { baseURL: `http://127.0.0.1:${ports.frontendPort}` },
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
