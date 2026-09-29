/**
 * Channel level meter worklet gate, run against the BUILT artifact.
 *
 * Same reasoning as the stretch-artifact gate next door: an AudioWorklet is
 * emitted as a separate asset through Vite's `?url` suffix, and only the built
 * output proves that the asset actually resolves and registers. A dev server
 * serves the file untransformed and would pass on code the installed app
 * cannot load.
 *
 * Run with::
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.meter-artifact.config.ts
 *
 * Requirements (mini-PRD):
 *
 * - ok Serve the real build output, never a dev server.
 *     [if] the config boots Vite [then STOP] the asset emit under test is skipped.
 * - ok Own a fixed port outside every reserved pair.
 *     [if] the port is one another lane owns [then STOP] the module loads.
 *
 * Acceptance tests:
 *
 * - [if] the port collides with a live lane service [then STOP] the run starts.
 * - [if] a server is already listening on this port [then STOP] the suite
 *   attaches to it, because it would then measure somebody else's build.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

/** This suite's own port, checked free at authoring time. */
export const METER_ARTIFACT_PORT = 5322;

/** Ports the primary checkout, live lane services and other suites already own. */
const RESERVED = new Set([
	8585, 5173, 8685, 9405, 8682, 9402, 5216, 9473, 8688, 9408, 5273, 8686,
	5399, 5311, 5320, 5214, 8690, 8692, 8695
]);

if (RESERVED.has(METER_ARTIFACT_PORT)) {
	throw new Error(
		`meter-artifact port ${METER_ARTIFACT_PORT} is owned by another lane or suite; pick another`
	);
}

export const METER_ARTIFACT_ORIGIN = `http://127.0.0.1:${METER_ARTIFACT_PORT}`;
export const METER_ARTIFACT_BUILD_DIR = `${FRONTEND_ROOT}build`;

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'meter-artifact.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	globalTimeout: 600_000,
	expect: { timeout: 10_000 },
	reporter: [['list']],
	webServer: {
		command: guardedWebServerCommand('meter-artifact-static', `pnpm build && python3 -m http.server ${METER_ARTIFACT_PORT} --bind 127.0.0.1 --directory build`),
		cwd: FRONTEND_ROOT,
		url: `${METER_ARTIFACT_ORIGIN}/index.html`,
		reuseExistingServer: false,
		timeout: 300_000
	},
	use: {
		baseURL: METER_ARTIFACT_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'meter-artifact-chromium',
			use: {
				...devices['Desktop Chrome'],
				launchOptions: {
					// A meter with no audio measures nothing, and this suite must
					// not depend on a user gesture to get a running context.
					args: ['--autoplay-policy=no-user-gesture-required']
				}
			}
		}
	]
});
