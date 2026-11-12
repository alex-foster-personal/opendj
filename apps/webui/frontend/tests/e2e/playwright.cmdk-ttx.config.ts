/**
 * Cmd K time-to-execute harness (issue #9468): 20 scripted commands on the real
 * engine with cmdk.v2 and cmdk.v2.actions on through the fixture flags.
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.cmdk-ttx.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { CMDK_TTX_API_PORT, CMDK_TTX_FRONTEND_PORT } from './vite.cmdk-ttx.config';
import { guardedWebServerCommand, shellArgument } from './support/guarded-web-server';
import { resolveProgram } from './support/resolve-program';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${CMDK_TTX_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${CMDK_TTX_API_PORT}`;
const FIXTURE_DATA_DIR = fileURLToPath(new URL('fixtures/cmdk-ttx-data', import.meta.url));
const SANDBOX_HOME = `${FIXTURE_DATA_DIR}-home`;
const NO_POSIX_LAUNCHER = process.platform === 'win32';
const UV = NO_POSIX_LAUNCHER ? 'uv' : resolveProgram('uv');
const PNPM = NO_POSIX_LAUNCHER ? 'pnpm' : resolveProgram('pnpm');
const FIXTURE_BUILDER = fileURLToPath(new URL('support/cmdk_ttx_fixture.py', import.meta.url));

export default defineConfig({
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: ['cmdk-ttx.spec.ts'],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 180_000,
	globalTimeout: 300_000,
	expect: { timeout: 15_000 },
	reporter: [['list']],
	...(NO_POSIX_LAUNCHER
		? {}
		: {
				webServer: [
					{
						command: guardedWebServerCommand(
							'cmdk-ttx-engine',
							[
								`${shellArgument(UV)} run --no-sync python ${shellArgument(FIXTURE_BUILDER)} --data-dir ${shellArgument(FIXTURE_DATA_DIR)} --home ${shellArgument(SANDBOX_HOME)}`,
								'&&',
								`${shellArgument(UV)} run --no-sync python -m apps.engine_core serve --data-dir ${shellArgument(FIXTURE_DATA_DIR)}`,
								'--host 127.0.0.1',
								`--port ${CMDK_TTX_API_PORT}`
							].join(' ')
						),
						cwd: REPOSITORY_ROOT,
						url: `${API_ORIGIN}/api/v1/health`,
						reuseExistingServer: false,
						timeout: 120_000,
						env: {
							...process.env,
							MDT_DATA_DIR: FIXTURE_DATA_DIR,
							MDT_LIBRARY_MODE: 'local',
							MDT_FEATURE_FLAGS_FILE: '',
							HOME: SANDBOX_HOME,
							WEB_CONCURRENCY: '',
							MUSIC_DJ_FRONTEND_PORT: String(CMDK_TTX_FRONTEND_PORT),
							MUSIC_DJ_BACKEND_PORT: String(CMDK_TTX_API_PORT)
						}
					},
					{
						command: guardedWebServerCommand(
							'cmdk-ttx-vite',
							`${shellArgument(PNPM)} exec vite --config tests/e2e/vite.cmdk-ttx.config.ts`
						),
						cwd: FRONTEND_ROOT,
						url: `${FRONTEND_ORIGIN}/performance`,
						reuseExistingServer: false,
						timeout: 90_000
					}
				]
			}),
	use: {
		baseURL: FRONTEND_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'cmdk-ttx-chromium', use: { ...devices['Desktop Chrome'] } }]
});
