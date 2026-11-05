/**
 * Real backend + committed bench clips for the silence watchdog e2e (#6636).
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.silence-expected-audible.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import {
	SILENCE_EXPECTED_AUDIBLE_API_PORT,
	SILENCE_EXPECTED_AUDIBLE_FRONTEND_PORT
} from './vite.silence-expected-audible.config';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${SILENCE_EXPECTED_AUDIBLE_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${SILENCE_EXPECTED_AUDIBLE_API_PORT}`;

export const FIXTURE_DATA_DIR = fileURLToPath(
	new URL('fixtures/silence-expected-audible-data', import.meta.url)
);

const FIXTURE_BUILDER = 'apps.webui.frontend.tests.e2e.support.silence_expected_audible_fixture';

export default defineConfig({
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: ['silence-expected-audible.spec.ts'],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 120_000,
	globalTimeout: 600_000,
	expect: { timeout: 20_000 },
	reporter: [['list']],
	webServer: [
		{
			command: guardedWebServerCommand(
				'silence-expected-audible-engine',
				[
					`mkdir -p ${FIXTURE_DATA_DIR}`,
					'&&',
					`uv run --no-sync python -m ${FIXTURE_BUILDER} --data-dir ${FIXTURE_DATA_DIR}`,
					'&&',
					'uv run --no-sync python -m apps.webui.server',
					'--host 127.0.0.1',
					`--port ${SILENCE_EXPECTED_AUDIBLE_API_PORT}`,
					'--prod'
				].join(' ')
			),
			cwd: REPOSITORY_ROOT,
			url: `${API_ORIGIN}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 180_000,
			env: {
				...process.env,
				MDT_DATA_DIR: FIXTURE_DATA_DIR,
				MDT_LIBRARY_MODE: 'local',
				MUSIC_DJ_FRONTEND_PORT: String(SILENCE_EXPECTED_AUDIBLE_FRONTEND_PORT),
				MUSIC_DJ_BACKEND_PORT: String(SILENCE_EXPECTED_AUDIBLE_API_PORT)
			}
		},
		{
			command: guardedWebServerCommand(
				'silence-expected-audible-vite',
				'pnpm exec vite --config tests/e2e/vite.silence-expected-audible.config.ts'
			),
			cwd: FRONTEND_ROOT,
			url: `${FRONTEND_ORIGIN}/performance`,
			reuseExistingServer: false,
			timeout: 120_000
		}
	],
	use: {
		baseURL: FRONTEND_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
		launchOptions: { args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'] }
	},
	projects: [{ name: 'silence-expected-audible-chromium', use: { ...devices['Desktop Chrome'] } }]
});
