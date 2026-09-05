/**
 * End-to-end UI evidence for the comment ('m') hotkey, against a REAL
 * backend (r3918992947): the prior browser spec set
 * `feedbackState.availability = 'ok'` directly, fabricating the exact state
 * the real /api/v1/feedback probe is responsible for establishing, so it
 * passed even if hydration, API compatibility, or production mounting never
 * makes the hotkey usable in the shipped app.
 *
 * No fixture library is needed: the feedback routes only ever read/write
 * <data-dir>/feedback/*.json, created on first write, so a throwaway EMPTY
 * data dir is enough for the real daemon to boot and answer the real probe.
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.comment-hotkey-gate.config.ts
 *
 * Requirements:
 *   - [if] the real availability probe never resolves 'ok' [then] the test
 *     times out waiting for it, not silently proceeding on fabricated state.
 *   - [if] the real hotkey handler is unwired from the real /performance
 *     page [then] this fails where the fabricated-state version could not.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import {
	COMMENT_HOTKEY_GATE_API_PORT,
	COMMENT_HOTKEY_GATE_FRONTEND_PORT
} from './vite.comment-hotkey-gate.config';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${COMMENT_HOTKEY_GATE_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${COMMENT_HOTKEY_GATE_API_PORT}`;

const FIXTURE_DATA_DIR = fileURLToPath(
	new URL('fixtures/comment-hotkey-gate-data', import.meta.url)
);

export default defineConfig({
	testDir: '.',
	testMatch: 'comment-hotkey-browser.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 30_000,
	globalTimeout: 180_000,
	expect: { timeout: 10_000 },
	reporter: [['list']],
	webServer: [
		{
			command: [
				// Wiped before every run, the same way
				// playwright.hotcue-mapping-gate.config.ts's fixture builder does:
				// without this, pins from a PRIOR run stay in
				// <data-dir>/feedback/comments.json and stack multiple `.fb-pin`s
				// at identical coordinates, so a later run's click can land on a
				// stale pin instead of the one the test just created.
				`rm -rf ${FIXTURE_DATA_DIR}`,
				'&&',
				`mkdir -p ${FIXTURE_DATA_DIR}`,
				'&&',
				'uv run --no-sync python -m apps.webui.server',
				'--host 127.0.0.1',
				`--port ${COMMENT_HOTKEY_GATE_API_PORT}`,
				'--prod'
			].join(' '),
			cwd: REPOSITORY_ROOT,
			url: `${API_ORIGIN}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 60_000,
			// A stray MDT_DATA_DIR from a lane .env must not outrank this
			// fixture dir; see playwright.hotcue-mapping-gate.config.ts.
			env: {
				...process.env,
				MDT_DATA_DIR: FIXTURE_DATA_DIR,
				MDT_LIBRARY_MODE: 'local'
			}
		},
		{
			command: 'pnpm exec vite --config tests/e2e/vite.comment-hotkey-gate.config.ts',
			cwd: FRONTEND_ROOT,
			url: `${FRONTEND_ORIGIN}/performance`,
			reuseExistingServer: false,
			timeout: 30_000
		}
	],
	use: {
		baseURL: FRONTEND_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'comment-hotkey-gate-chromium', use: { ...devices['Desktop Chrome'] } }]
});

export { COMMENT_HOTKEY_GATE_API_PORT, COMMENT_HOTKEY_GATE_FRONTEND_PORT };
