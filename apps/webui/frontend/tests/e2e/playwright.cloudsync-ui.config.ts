/**
 * /cloudsync UI e2e (plan W17): a real hub engine, a real spoke engine over a
 * real fixture library, and vite in front of the spoke. Nothing is mocked:
 * Sync now crosses a socket from the spoke to the hub.
 *
 *   hub   engine_core serve, MDT_IS_HUB=1, freshly initialized state.db
 *   spoke engine_core serve over the deckload fixture library (real tracks)
 *   vite  proxies /api to the spoke
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.cloudsync-ui.config.ts
 *
 * Requirements:
 *   - OK Fixed loopback ports outside the lane do-not-bind list and every
 *     other suite's configured ports (8711, 8712, 5331).
 *   - OK Both data dirs are throwaway fixtures reset on every run; no real
 *     user database is ever opened.
 *   - OK Inherited MDT_CLOUDSYNC_* env is blanked so the page, not the
 *     parent shell, decides the config under test.
 *   - OK No retries.
 */
import { defineConfig, devices } from '@playwright/test';
import { mkdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FIXTURES = fileURLToPath(new URL('fixtures', import.meta.url));

export const CLOUDSYNC_UI_HUB_PORT = 8711;
export const CLOUDSYNC_UI_SPOKE_PORT = 8712;
export const CLOUDSYNC_UI_FRONTEND_PORT = 5331;
export const CLOUDSYNC_UI_ORIGIN = `http://127.0.0.1:${CLOUDSYNC_UI_FRONTEND_PORT}`;
export const CLOUDSYNC_UI_HUB_URL = `http://127.0.0.1:${CLOUDSYNC_UI_HUB_PORT}`;

const HUB_DATA_DIR = join(FIXTURES, 'cloudsync-ui-hub-data');
const SPOKE_DATA_DIR = join(FIXTURES, 'cloudsync-ui-spoke-data');
for (const dir of [HUB_DATA_DIR, SPOKE_DATA_DIR]) {
	if (dirname(dir) !== FIXTURES) throw new Error(`Refusing to reset outside ${FIXTURES}: ${dir}`);
	if (process.env.TEST_WORKER_INDEX === undefined) rmSync(dir, { recursive: true, force: true });
	mkdirSync(join(dir, 'sandbox-home'), { recursive: true });
}

const FIXTURE_BUILDER = fileURLToPath(new URL('support/deckload_fixture.py', import.meta.url));
const VITE_CONFIG = fileURLToPath(new URL('vite.cloudsync-ui.config.ts', import.meta.url));

function engineCommand(dataDir: string, port: number): string {
	return `uv run --no-sync python -m apps.engine_core serve --data-dir ${dataDir} --host 127.0.0.1 --port ${port}`;
}

function engineEnv(dataDir: string, isHub: '1' | '0'): Record<string, string> {
	return {
		...(process.env as Record<string, string>),
		MDT_DATA_DIR: dataDir,
		MDT_LIBRARY_MODE: 'local',
		MDT_IS_HUB: isHub,
		MDT_CLOUDSYNC_SCHEDULER: '',
		MDT_CLOUDSYNC_HUB_URL: '',
		MDT_CLOUDSYNC_SIGNED_IN_AS: '',
		WEB_CONCURRENCY: '',
		HOME: join(dataDir, 'sandbox-home')
	};
}

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'cloudsync-ui.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 150_000,
	expect: { timeout: 15_000 },
	reporter: [['list']],
	webServer: [
		{
			command: guardedWebServerCommand('cloudsync-ui-engine', `uv run --no-sync python -m apps.shared.state.cli init && ${engineCommand(HUB_DATA_DIR, CLOUDSYNC_UI_HUB_PORT)}`),
			cwd: REPOSITORY_ROOT,
			url: `${CLOUDSYNC_UI_HUB_URL}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 180_000,
			env: engineEnv(HUB_DATA_DIR, '1')
		},
		{
			command: guardedWebServerCommand('cloudsync-ui-engine-2', `uv run --no-sync python ${FIXTURE_BUILDER} --data-dir ${SPOKE_DATA_DIR} && ${engineCommand(SPOKE_DATA_DIR, CLOUDSYNC_UI_SPOKE_PORT)}`),
			cwd: REPOSITORY_ROOT,
			url: `http://127.0.0.1:${CLOUDSYNC_UI_SPOKE_PORT}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 180_000,
			env: engineEnv(SPOKE_DATA_DIR, '0')
		},
		{
			command: guardedWebServerCommand('cloudsync-ui-vite', `pnpm exec vite --config ${VITE_CONFIG}`),
			cwd: FRONTEND_ROOT,
			url: `${CLOUDSYNC_UI_ORIGIN}/`,
			reuseExistingServer: false,
			timeout: 90_000,
			env: {
				...(process.env as Record<string, string>),
				CLOUDSYNC_UI_FRONTEND_PORT: String(CLOUDSYNC_UI_FRONTEND_PORT),
				CLOUDSYNC_UI_API_PORT: String(CLOUDSYNC_UI_SPOKE_PORT)
			}
		}
	],
	use: { trace: 'retain-on-failure', screenshot: 'only-on-failure' },
	projects: [{ name: 'cloudsync-ui-chromium', use: { ...devices['Desktop Chrome'] } }]
});
