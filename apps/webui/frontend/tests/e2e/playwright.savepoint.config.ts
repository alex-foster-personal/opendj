/**
 * Savepoint smoke gate: one server boot, one browser page, six serial checks
 * against the REAL library, budgeted at roughly two minutes wall clock.
 *
 * Requirements:
 *
 * - ✔︎ Ports come from this worktree's claimed pair, never 8585/5173.
 * - ✔︎ The backend reads the primary checkout's data dir via ``MDT_DATA_DIR``.
 * - ✔︎ One backend + one Vite boot is shared by every test (serial, 1 worker).
 * - ✔︎ No retries and a hard global timeout, so a hung gate fails fast.
 *
 * Acceptance tests:
 *
 * - [if] either claimed port is already bound [then ⛔️] the run starts.
 * - [if] the suite exceeds the global timeout [then ⛔️] it reports success.
 * - [if] a test fails [then ⛔️] Playwright retries it and hides the flake.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import {
	claimWebuiPorts,
	requireLoopbackOrigin,
	requireRealLibraryDataDir
} from './savepoint-endpoints';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

const ports = claimWebuiPorts(REPOSITORY_ROOT);
const frontend = requireLoopbackOrigin(
	'SAVEPOINT_SMOKE_BASE_URL',
	process.env.SAVEPOINT_SMOKE_BASE_URL ?? `http://127.0.0.1:${ports.frontendPort}`
);
const api = requireLoopbackOrigin(
	'SAVEPOINT_SMOKE_API_BASE',
	process.env.SAVEPOINT_SMOKE_API_BASE ?? `http://127.0.0.1:${ports.backendPort}`
);
const dataDir = requireRealLibraryDataDir(REPOSITORY_ROOT);

// ENGINE_CMD seam: the backend boot command is a template so the same spec
// runs against either engine. Placeholders {host} {port} {data_dir} are
// substituted here; unset means the legacy webui server, verbatim as before.
const engineCmd = (
	process.env.ENGINE_CMD ??
	'uv run --no-sync python -m apps.webui.server --host {host} --port {port} --prod'
)
	.replaceAll('{host}', api.hostname)
	.replaceAll('{port}', String(api.port))
	.replaceAll('{data_dir}', dataDir);

/**
 * Which daemon is booting, as far as GET /api/v1/build-info is concerned.
 *
 * Only the rebuilt engine serves that route (apps/engine_core/build_info.py).
 * The legacy webui server predates it and answers 404. The frontend asks for
 * it on every page (lib/rb/build-identity.ts) and renders the 404 as a named
 * "this daemon predates the build stamp" fault, deliberately and with no
 * fallback -- so under the legacy engine that 404 is correct behaviour, not a
 * regression. The smoke's blanket "no failed requests" gate counted it as a
 * problem anyway, which left this gate red on pristine main for every caller.
 *
 * A flag rather than a blanket exemption in the spec, because the engine that
 * DOES owe a build identity must still fail the gate when it stops serving
 * one. Consumed by EXPECTED_RESOURCE_FAILURES in savepoint-smoke.spec.ts.
 */
const engineServesBuildInfo = engineCmd.includes('apps.engine_core');

// Playwright re-evaluates this config inside every worker, so exporting the
// resolved origins here is what makes them visible to the spec itself.
process.env.SAVEPOINT_SMOKE_BASE_URL = frontend.origin;
process.env.SAVEPOINT_SMOKE_API_BASE = api.origin;
process.env.SAVEPOINT_SMOKE_ENGINE_SERVES_BUILD_INFO = engineServesBuildInfo ? '1' : '';

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'savepoint-smoke.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	globalTimeout: 210_000,
	expect: { timeout: 10_000 },
	reporter: [['list']],
	webServer: [
		{
			command: guardedWebServerCommand('savepoint-engine', engineCmd),
			cwd: REPOSITORY_ROOT,
			url: `${api.origin}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 90_000,
			env: { ...process.env, MDT_DATA_DIR: dataDir }
		},
		{
			command: guardedWebServerCommand('savepoint-vite', 'pnpm exec vite --config tests/e2e/vite.savepoint.config.ts'),
			cwd: FRONTEND_ROOT,
			url: `${frontend.origin}/performance`,
			reuseExistingServer: false,
			timeout: 90_000,
			env: {
				...process.env,
				SAVEPOINT_SMOKE_BASE_URL: frontend.origin,
				SAVEPOINT_SMOKE_API_BASE: api.origin,
				VITE_PERFORMANCE_AUDIO_ACTIVATION: 'auto'
			}
		}
	],
	use: {
		baseURL: frontend.origin,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
		launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] }
	},
	projects: [
		{
			name: 'savepoint-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } }
		}
	]
});
