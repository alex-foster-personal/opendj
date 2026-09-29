/**
 * End-to-end proof that a sync-triggering control is inert while the daemon
 * runs in one-way import mode.
 *
 * One Vite boot, no backend, no real library: the spec stubs every API call,
 * so this gate can run anywhere and can never reach a rekordbox target.
 *
 * Run with::
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.rekordbox-gate.config.ts
 *
 * Requirements:
 *   - ✔︎ Fixed loopback port, never 5173/8585/8685/9405.
 *   - ✔︎ No retries, so an inert control that flakes into life is not hidden.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { REKORDBOX_GATE_E2E_PORT } from './vite.rekordbox-gate.config';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const ORIGIN = `http://127.0.0.1:${REKORDBOX_GATE_E2E_PORT}`;

export default defineConfig({
	testDir: '.',
	testMatch: [
		'rekordbox-writeback-disabled.spec.ts',
		'artwork-reader-unavailable.spec.ts',
		'missing-tracks-folder.spec.ts'
	],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	globalTimeout: 180_000,
	reporter: [['list']],
	webServer: {
		command: guardedWebServerCommand('rekordbox-gate-vite', 'pnpm exec vite --config tests/e2e/vite.rekordbox-gate.config.ts'),
		cwd: FRONTEND_ROOT,
		url: `${ORIGIN}/reconcile`,
		reuseExistingServer: false,
		timeout: 120_000
	},
	use: { baseURL: ORIGIN, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
	projects: [{ name: 'rekordbox-gate-chromium', use: { ...devices['Desktop Chrome'] } }]
});
