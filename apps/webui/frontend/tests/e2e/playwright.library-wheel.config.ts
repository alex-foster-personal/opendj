/**
 * Dedicated LIBUX-06 Library Wheel e2e: a REAL engine over a REAL
 * genre-family fixture (state.db + master.plain.db).
 *
 * The root Playwright deckload library has no rekordbox vendor mapping and
 * no master.plain.db, so GET /api/v1/library/wheel 503s there. That path is
 * the unavailable branch, not "renders the real library grouped by genre".
 * This config boots the production engine against the 5-track wheel fixture
 * instead of stubbing the route.
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.library-wheel.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FIXTURE_DATA_DIR = join(REPOSITORY_ROOT, '.tmp', 'library-wheel-e2e-data');

const ENGINE_COMMAND = [
	`uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.library_wheel_fixture --data-dir ${FIXTURE_DATA_DIR}`,
	'&&',
	'uv run --no-sync python -m apps.engine_core serve',
	`--data-dir ${FIXTURE_DATA_DIR}`,
	'--host 127.0.0.1',
	'--port 9428'
].join(' ');

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'library-wheel.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 30_000,
	expect: { timeout: 10_000 },
	webServer: [
		{
			command: guardedWebServerCommand('library-wheel-engine', ENGINE_COMMAND),
			cwd: REPOSITORY_ROOT,
			url: 'http://127.0.0.1:9428/api/v1/health',
			reuseExistingServer: false,
			timeout: 120_000,
			// The pairing lets the page's own POSTs (the page-view client
			// event) pass the mutating-origin guard (#2689), which otherwise
			// 403s any Origin but the daemon's paired frontend; the spec
			// asserts no failed resources.
			env: {
				...process.env,
				MDT_DATA_DIR: FIXTURE_DATA_DIR,
				MDT_LIBRARY_MODE: 'local',
				MUSIC_DJ_FRONTEND_PORT: '5228',
				MUSIC_DJ_BACKEND_PORT: '9428',
				WEB_CONCURRENCY: ''
			}
		},
		{
			command: guardedWebServerCommand('library-wheel-vite', 'pnpm exec vite --config tests/e2e/vite.library-wheel.config.ts'),
			cwd: FRONTEND_ROOT,
			url: 'http://127.0.0.1:5228/library-wheel',
			reuseExistingServer: false,
			timeout: 60_000
		}
	],
	use: {
		baseURL: 'http://127.0.0.1:5228',
		viewport: { width: 1280, height: 800 },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'library-wheel-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } }
		}
	]
});
