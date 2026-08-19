/**
 * WebKit performance-controls gate against the PRODUCTION artifact.
 *
 * Playwright's webkit project shares the WKWebView engine core, so this is
 * the only gate in the repo that reproduces the class of failure where the
 * installed desktop app was broken while every chromium/dev-server gate
 * stayed green (blob: AudioWorklet modules, esbuild-lowered class fields in
 * a self-stringifying worklet). Two properties are load bearing and neither
 * is negotiable:
 *
 *   - webkit, not chromium.
 *   - the SPA is served by the ENGINE from `apps/webui/frontend/build`, not
 *     by vite. Dev serves packages untransformed; the artifact does not.
 *
 * The library is a throwaway fixture built by the real folder ingest over
 * generated audio (see support/deckload_fixture.py). It is NOT the lane data
 * dir: the engine takes a singleton lock, so a suite that borrowed the lane's
 * dir could not run while the lane's own engine was up.
 *
 * Requirements:
 *
 * - ✔︎ One fixed loopback port (8690), never any port another lane claims.
 * - ✔︎ Fixture data dir lives inside this worktree, never a shared data dir.
 * - ✔︎ The production build must already exist; a stale/absent build fails at
 *   config load with the command to run, never mid-test as a mystery.
 * - ✔︎ No retries and one worker: a flaky worklet is the defect under test.
 *
 * Acceptance tests:
 *
 * - [if] `build/index.html` is missing [then ⛔️] the run starts.
 * - [if] the port is one of the reserved lane ports [then ⛔️] the config loads.
 * - [if] a test fails [then ⛔️] Playwright retries it and hides the flake.
 */
import { defineConfig, devices } from '@playwright/test';
import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

/** This suite's own port. Fixed, so two concurrent runs collide loudly. */
export const WEBKIT_DECKLOAD_PORT = 8690;

/**
 * Ports other lanes/worktrees have claimed. Binding one of these would take a
 * live service down, so the collision is refused at config load rather than
 * discovered by whoever was using it.
 */
const RESERVED_PORTS: readonly number[] = [
	8585, 5173, 8685, 9405, 8682, 9402, 5216, 9473, 8688, 9408, 5273, 8686, 5399, 5311
];

if (RESERVED_PORTS.includes(WEBKIT_DECKLOAD_PORT)) {
	throw new Error(
		`webkit-deckload port ${WEBKIT_DECKLOAD_PORT} is claimed by another lane; pick a free one`
	);
}

export const WEBKIT_DECKLOAD_ORIGIN = `http://127.0.0.1:${WEBKIT_DECKLOAD_PORT}`;

/** Throwaway library the engine serves. Inside this worktree, gitignored. */
export const FIXTURE_DATA_DIR = join(
	FRONTEND_ROOT,
	'tests',
	'e2e',
	'fixtures',
	'deckload-data'
);

/**
 * HOME for the engine under test, and the reason it exists.
 *
 * apps/shared/platform_paths.py derives BOTH the rekordbox app dir
 * (~/Library/Pioneer/rekordbox) and the default music root (~/Music) from
 * HOME. A fixture engine that keeps the real HOME can therefore be pointed at
 * the real library by any code path that resolves those defaults, and one was:
 * a `setup.import-rekordbox` job ran inside the SIBLING tier-2 fixture dir,
 * snapshotted and decrypted the live master.db and wrote 32 real tracks into
 * what is supposed to be a two-track generated library (Wed 19 Aug 2026).
 * Read-only toward rekordbox, and the builder's count check caught it on the
 * next run, but the fixture's whole claim is that no real audio is reachable.
 * It is now unreachable by construction rather than by intent.
 */
const SANDBOX_HOME = join(FIXTURE_DATA_DIR, 'sandbox-home');
mkdirSync(SANDBOX_HOME, { recursive: true });

const BUILD_INDEX = join(FRONTEND_ROOT, 'build', 'index.html');
if (!existsSync(BUILD_INDEX)) {
	throw new Error(
		`webkit-deckload needs the production build: ${BUILD_INDEX} does not exist. ` +
			'Run `pnpm build` in apps/webui/frontend first (or use `just webkit-deckload-e2e`, ' +
			'which builds before it runs). This suite deliberately does NOT use vite.'
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
// the engine opens it, and Playwright starts webServers BEFORE globalSetup, so
// a globalSetup hook would be too late.
const ENGINE_COMMAND = [
	`uv run --no-sync python ${FIXTURE_BUILDER} --data-dir ${FIXTURE_DATA_DIR}`,
	'&&',
	'uv run --no-sync python -m apps.engine_core serve',
	`--data-dir ${FIXTURE_DATA_DIR}`,
	'--host 127.0.0.1',
	`--port ${WEBKIT_DECKLOAD_PORT}`
].join(' ');

export default defineConfig({
	testDir: '.',
	testMatch: 'webkit-deckload.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 120_000,
	globalTimeout: 900_000,
	expect: { timeout: 20_000 },
	reporter: [['list']],
	webServer: {
		command: ENGINE_COMMAND,
		cwd: REPOSITORY_ROOT,
		url: `${WEBKIT_DECKLOAD_ORIGIN}/api/v1/health`,
		reuseExistingServer: false,
		timeout: 180_000,
		// The engine refuses to boot with WEB_CONCURRENCY set, and a stray
		// MDT_DATA_DIR from a lane .env must not outrank --data-dir.
		env: {
			...process.env,
			MDT_DATA_DIR: FIXTURE_DATA_DIR,
			WEB_CONCURRENCY: '',
			HOME: SANDBOX_HOME
		}
	},
	use: {
		baseURL: WEBKIT_DECKLOAD_ORIGIN,
		viewport: { width: 1600, height: 1000 },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
		video: 'off'
	},
	projects: [
		{
			name: 'webkit',
			use: { ...devices['Desktop Safari'], viewport: { width: 1600, height: 1000 } }
		}
	]
});
