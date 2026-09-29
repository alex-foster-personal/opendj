/**
 * Library jobs ordering gate (#1975): real daemon, dry runner, no Modal GPU.
 *
 * Run locally:
 *   pnpm exec playwright test --config tests/e2e/playwright.library-jobs.config.ts --headed
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { resolveEndpoints, seedDataDir } from './library-jobs-e2e-endpoints';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

const endpoints = resolveEndpoints();
const dataDir = seedDataDir(REPOSITORY_ROOT);

process.env.LIBRARY_JOBS_E2E_API_BASE = endpoints.backendOrigin;
process.env.LIBRARY_JOBS_E2E_FRONTEND_PORT = String(endpoints.frontendPort);
process.env.LIBRARY_JOBS_E2E_DATA_DIR = dataDir;

// MUSIC_DJ_FRONTEND_PORT / MUSIC_DJ_BACKEND_PORT tell the daemon which
// frontend it is paired with. Since the mutating-origin guard landed
// (apps/webui/server/request_guard.py, issue #2689) the daemon 403s
// ORIGIN_NOT_ALLOWED on every POST/PUT/PATCH/DELETE whose Origin is not its
// own paired frontend, and this suite's backend was only ever told its own
// --port: it resolved the frontend port from the checkout's root .env
// (whatever pair this worktree happens to have claimed), never 5277, so every
// "do next" enqueue was refused. The lane then read back empty and the
// ordering assertion compared [] against the two selected ids. The GETs this
// spec polls are not mutating, so they kept answering 200 and the failure
// surfaced as an ordering mismatch rather than as the 403 it was.
//
// Declaring the pairing is the same mechanism the shipped dev server uses; it
// grants this suite's origin and nothing else, so a foreign origin is still
// refused.
const engineEnv = {
	...process.env,
	MDT_DATA_DIR: dataDir,
	MDT_LIBRARY_MODE: 'local',
	MUSIC_DJ_FRONTEND_PORT: String(endpoints.frontendPort),
	MUSIC_DJ_BACKEND_PORT: String(endpoints.backendPort),
	MUSIC_DJ_LIBRARY_JOBS: 'on',
	MUSIC_DJ_LIBRARY_JOBS_RUNNER: 'dry',
	// Two-sided, and the spec (specs/d4a29afe_stems-lyrics-e2e.md) names the
	// lower bound: the hold must exceed the time from the menu click to the
	// GET and the panel asserts, or a job settles mid-interaction and the
	// lane reads idle. 2.5s met that on a quiet machine and not on a loaded
	// CI host, where opening the panel alone can take seconds. The upper
	// bound is the NEXT test: a cancel marks the row but does not interrupt
	// the runner's sleep, so the hold is also the worst-case wait before the
	// next test's job is claimed, and it has to stay inside that test's 15s
	// expect budget.
	MUSIC_DJ_LIBRARY_JOBS_DRY_HOLD_S: '8'
};

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
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
			command: guardedWebServerCommand('library-jobs-engine', `uv run --no-sync python -m apps.webui.server --host 127.0.0.1 --port ${endpoints.backendPort} --prod`),
			cwd: REPOSITORY_ROOT,
			url: `${endpoints.backendOrigin}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: engineEnv
		},
		{
			command: guardedWebServerCommand('library-jobs-vite', 'pnpm exec vite --config tests/e2e/vite.library-jobs.config.ts'),
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
