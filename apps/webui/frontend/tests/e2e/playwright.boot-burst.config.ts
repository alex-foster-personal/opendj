/**
 * PERF-R6 boot-burst bench: what a deck load costs when it is fired while the
 * app is still starting.
 *
 * This is a MEASUREMENT lane, not a gate. It exists because the regression it
 * measures is invisible to every other suite: steady-state deck loads are
 * fine (fetchWall 86ms), and only a load fired inside the startup request
 * burst pays the 2.1x (fetchWall median 3793 -> 8038ms against the 18 Aug
 * build). Two properties make the number mean something and neither is
 * negotiable:
 *
 *   - the SPA is the PRODUCTION build served by the ENGINE, not vite. Vite
 *     serves hundreds of unbundled modules, which drowns the very connection
 *     contention this bench is about.
 *   - the engine is the real single-worker daemon (workers=1 is locked by
 *     design), because the queueing behind one worker is half the mechanism.
 *
 * It shares the webkit lane's throwaway fixture builder (generated audio,
 * ingested by the real folder adapter) rather than inventing another one, and
 * runs chromium because the browser's six-connections-per-origin pool is the
 * other half of the mechanism and chromium is the reference for it.
 *
 * Run it:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.boot-burst.config.ts
 *
 * Requirements:
 *
 * - OK The production build must already exist; a missing build fails at
 *   config load with the command to run, never mid-run as a mystery.
 * - OK Its own loopback port, never one another lane has claimed.
 * - OK Fixture data dir inside this worktree, never a shared data dir.
 * - OK One worker, no retries: a bench that retried would report the fastest
 *   run of a flaky pair as if it were the number.
 *
 * Acceptance tests:
 *
 * - [if] `build/index.html` is missing [then] the run refuses to start.
 * - [if] the port collides with a reserved lane port [then] the config throws.
 * - [if] Playwright retries a run [then] the medians are not comparable.
 */
import { defineConfig, devices } from '@playwright/test';
import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

/** This bench's own port. Overridable so the A and B arms of a comparison can
 * hold two engines up at once without either being the odd one out. */
export const BOOT_BURST_PORT = Number(process.env.BOOT_BURST_PORT ?? 8692);

/** Ports other lanes/worktrees have claimed, including the tier-1 webkit
 * suite's 8690. Binding one would take a live service down. */
const RESERVED_PORTS: readonly number[] = [
	8585, 5173, 8685, 9405, 8682, 9402, 5216, 9473, 8688, 9408, 5273, 8686, 5399, 5311, 8690
];

if (RESERVED_PORTS.includes(BOOT_BURST_PORT)) {
	throw new Error(`boot-burst port ${BOOT_BURST_PORT} is claimed by another lane; pick a free one`);
}

export const BOOT_BURST_ORIGIN = `http://127.0.0.1:${BOOT_BURST_PORT}`;

/** Throwaway library the engine serves. Inside this worktree, gitignored.
 * Its own dir, so a bench run cannot collide with the webkit suite's engine
 * over the data dir's singleton lock. */
export const FIXTURE_DATA_DIR = join(FRONTEND_ROOT, 'tests', 'e2e', 'fixtures', 'boot-burst-data');

/** HOME for the engine under test. apps/shared/platform_paths.py derives both
 * the rekordbox app dir and the default music root from HOME, so a fixture
 * engine that kept the real HOME could reach the real library through any
 * path that resolves those defaults. Unreachable by construction instead. */
const SANDBOX_HOME = join(FIXTURE_DATA_DIR, 'sandbox-home');
mkdirSync(SANDBOX_HOME, { recursive: true });

const BUILD_INDEX = join(FRONTEND_ROOT, 'build', 'index.html');
if (!existsSync(BUILD_INDEX)) {
	throw new Error(
		`boot-burst needs the production build: ${BUILD_INDEX} does not exist. ` +
			'Run `pnpm build` in apps/webui/frontend first. This bench deliberately does NOT use vite.'
	);
}

const FIXTURE_BUILDER = join(
	'apps',
	'webui',
	'frontend',
	'tests',
	'e2e',
	'support',
	'deckload_fixture.py'
);

// One shell command, two ordered steps: the fixture library must exist before
// the engine opens it, and Playwright starts webServers BEFORE globalSetup.
const ENGINE_COMMAND = [
	`uv run --no-sync python ${FIXTURE_BUILDER} --data-dir ${FIXTURE_DATA_DIR}`,
	'&&',
	'uv run --no-sync python -m apps.engine_core serve',
	`--data-dir ${FIXTURE_DATA_DIR}`,
	'--host 127.0.0.1',
	`--port ${BOOT_BURST_PORT}`
].join(' ');

export default defineConfig({
	testDir: '.',
	testMatch: ['boot-burst.spec.ts'],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 600_000,
	globalTimeout: 1_200_000,
	expect: { timeout: 30_000 },
	reporter: [['list']],
	webServer: {
		command: guardedWebServerCommand('boot-burst-engine', ENGINE_COMMAND),
		cwd: REPOSITORY_ROOT,
		url: `${BOOT_BURST_ORIGIN}/api/v1/health`,
		reuseExistingServer: false,
		timeout: 180_000,
		env: {
			...process.env,
			MDT_DATA_DIR: FIXTURE_DATA_DIR,
			WEB_CONCURRENCY: '',
			HOME: SANDBOX_HOME
		}
	},
	use: {
		baseURL: BOOT_BURST_ORIGIN,
		viewport: { width: 1600, height: 1000 },
		trace: 'off',
		screenshot: 'off',
		video: 'off'
	},
	projects: [
		{
			name: 'chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1600, height: 1000 } }
		}
	]
});
