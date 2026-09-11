/**
 * Library jobs ordering gate (#1975): real daemon, dry runner, no Modal GPU.
 *
 * Run locally:
 *   pnpm exec playwright test --config tests/e2e/playwright.library-jobs.config.ts --headed
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { resolveEndpoints, seedDataDir } from './library-jobs-e2e-endpoints';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

const endpoints = resolveEndpoints();
const dataDir = seedDataDir(REPOSITORY_ROOT);

process.env.LIBRARY_JOBS_E2E_API_BASE = endpoints.backendOrigin;
process.env.LIBRARY_JOBS_E2E_FRONTEND_PORT = String(endpoints.frontendPort);
process.env.LIBRARY_JOBS_E2E_DATA_DIR = dataDir;

const engineEnv = {
	...process.env,
	MDT_DATA_DIR: dataDir,
	MDT_LIBRARY_MODE: 'local',
	MUSIC_DJ_LIBRARY_JOBS: 'on',
	MUSIC_DJ_LIBRARY_JOBS_RUNNER: 'dry',
	MUSIC_DJ_LIBRARY_JOBS_DRY_HOLD_S: '2.5'
};

export default defineConfig({
	testDir: '.',
	testMatch: 'library-jobs-ordering.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 90_000,
	globalTimeout: 300_000,
	expect: { timeout: 15_000 },
	reporter: [['list']],
	webServer: [
		{
			command: `uv run --no-sync python -m apps.webui.server --host 127.0.0.1 --port ${endpoints.backendPort} --prod`,
			cwd: REPOSITORY_ROOT,
			url: `${endpoints.backendOrigin}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: engineEnv
		},
		{
			command: 'pnpm exec vite --config tests/e2e/vite.library-jobs.config.ts',
			cwd: FRONTEND_ROOT,
			url: `${endpoints.frontendOrigin}/performance`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: {
				...process.env,
				LIBRARY_JOBS_E2E_API_BASE: endpoints.backendOrigin,
				LIBRARY_JOBS_E2E_FRONTEND_PORT: String(endpoints.frontendPort)
			}
		}
	],
	use: {
		baseURL: endpoints.frontendOrigin,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'library-jobs-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } }
		}
	]
});
