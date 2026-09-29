/**
 * The stems progress gate: a REAL engine, a REAL job, a REAL browser.
 *
 * The only substitution anywhere in this run is the Modal call itself, through
 * the worker's declared MDT_STEMS_SEPARATOR seam. Everything between the
 * button and the bundle -- enqueue over HTTP, the runner's fork, the stdout
 * progress protocol, library.changed, the WS hub, the jobs store, the TopBar
 * component -- is production code.
 *
 * Two things this config does NOT do, both on purpose:
 *
 * - it does not claim worktree ports. The claim helper writes the root `.env`
 *   and this lane must not touch it. Ports come from STEMS_E2E_* with a
 *   default pair that was free at authoring time, and strictPort means a
 *   collision fails the run rather than quietly attaching to somebody else's
 *   server.
 * - it does not point at a real library. This suite ENQUEUES WORK and writes
 *   stem bundles, so it builds its own throwaway data dir under the OS temp
 *   root. Nothing here can reach the primary checkout's data, the lane data
 *   dir, or an existing bundle.
 *
 * Requirements:
 *   ✔︎ ✅ one engine boot and one Vite boot shared by the suite, serial.
 *   ✔︎ ✅ no retries: a flake must show as a flake, not be hidden.
 *   [if] either port is already bound [then ⛔️] the run starts.
 *   [if] the separator seam is unset [then] the run would bill a real GPU, so
 *     it is set here rather than left to the environment.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { resolveEndpoints, seedDataDir } from './stems-e2e-endpoints';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

const endpoints = resolveEndpoints();
const dataDir = seedDataDir(REPOSITORY_ROOT);

// Playwright re-evaluates this config in every worker, so the resolved values
// travel to the spec and the Vite config through the environment.
process.env.STEMS_E2E_API_BASE = endpoints.backendOrigin;
process.env.STEMS_E2E_FRONTEND_PORT = String(endpoints.frontendPort);
process.env.STEMS_E2E_DATA_DIR = dataDir;

const engineEnv = {
	...process.env,
	MDT_DATA_DIR: dataDir,
	MDT_LIBRARY_MODE: 'local',
	// THE ONLY SUBSTITUTION. Set here so the suite cannot accidentally run
	// against the real GPU because somebody forgot to export it.
	MDT_STEMS_SEPARATOR: 'tests.stems.modal_separator_double:separate'
};

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: process.env.STEMS_E2E_MATCH ?? 'stems-progress.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 90_000,
	globalTimeout: 300_000,
	expect: { timeout: 20_000 },
	reporter: [['list']],
	webServer: [
		{
			command: guardedWebServerCommand('stems-engine', `uv run --no-sync python -m apps.engine_core serve --data-dir ${dataDir} --host 127.0.0.1 --port ${endpoints.backendPort}`),
			cwd: REPOSITORY_ROOT,
			url: `${endpoints.backendOrigin}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: engineEnv
		},
		{
			command: guardedWebServerCommand('stems-vite', 'pnpm exec vite --config tests/e2e/vite.stems.config.ts'),
			cwd: FRONTEND_ROOT,
			url: `${endpoints.frontendOrigin}/performance`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: {
				...process.env,
				STEMS_E2E_API_BASE: endpoints.backendOrigin,
				STEMS_E2E_FRONTEND_PORT: String(endpoints.frontendPort)
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
			name: 'stems-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } }
		}
	]
});
