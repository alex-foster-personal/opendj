/**
 * Cmd K pick-race gate (CMDK-16, issue #6091; Codex r4266506832 on #6089):
 * the pick race run against the real engine, decoder and a real fixture
 * library, with `cmdk.v2` on through the data dir's own feature-flags.json.
 *
 * The fixture (support/cmdk_pick_race_fixture.py) is rebuilt on every run so
 * the engine starts cold: past the per-page stat budget its rows are served
 * AVAILABILITY_PENDING on the first index, and a Cmd K pick of one must settle
 * through rb-meta, which is the wait the spec holds open. Nothing is mocked or
 * rewritten: the hold delays the real rb-meta response and then lets it
 * through unchanged.
 *
 * The engine starts through `guardedWebServerCommand`, which every e2e config
 * here shares and which launches `/bin/sh` and `.venv/bin/python`: POSIX-only
 * today. So on Windows this config starts no server and the spec skips, naming
 * that dependency (AGENTS.md "Portable by default"), rather than failing at
 * startup.
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.cmdk-pick-race.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { CMDK_PICK_RACE_API_PORT, CMDK_PICK_RACE_FRONTEND_PORT } from './vite.cmdk-pick-race.config';
import { guardedWebServerCommand, shellArgument } from './support/guarded-web-server';
import { resolveProgram } from './support/resolve-program';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${CMDK_PICK_RACE_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${CMDK_PICK_RACE_API_PORT}`;
const FIXTURE_DATA_DIR = fileURLToPath(new URL('fixtures/cmdk-pick-race-data', import.meta.url));
const SANDBOX_HOME = `${FIXTURE_DATA_DIR}-home`;
/** The shared launcher (`guardedWebServerCommand`) is POSIX-only: see the header. */
const NO_POSIX_LAUNCHER = process.platform === 'win32';
/** uv, resolved once: every step launches this path, not a bare name the PATH finds later (Codex P1, #6689). */
const UV = NO_POSIX_LAUNCHER ? 'uv' : resolveProgram('uv');
/** pnpm, resolved once for the Vite server step (Codex P1, #6689). */
const PNPM = NO_POSIX_LAUNCHER ? 'pnpm' : resolveProgram('pnpm');
const FIXTURE_BUILDER = fileURLToPath(new URL('support/cmdk_pick_race_fixture.py', import.meta.url));

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: ['cmdk-pick-race.spec.ts'],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 120_000,
	globalTimeout: 300_000,
	expect: { timeout: 15_000 },
	reporter: [['list']],
	...(NO_POSIX_LAUNCHER ? {} : { webServer: [
		{
			command: guardedWebServerCommand('cmdk-pick-race-engine', [
				// The fixture builder (Python) also creates the sandbox HOME: no shell-specific step here.
				`${shellArgument(UV)} run --no-sync python ${shellArgument(FIXTURE_BUILDER)} --data-dir ${shellArgument(FIXTURE_DATA_DIR)} --home ${shellArgument(SANDBOX_HOME)}`,
				'&&',
				// The engine, not apps.webui.server: it serves /api/v1/flags, which turns the v2 bar on.
				`${shellArgument(UV)} run --no-sync python -m apps.engine_core serve --data-dir ${shellArgument(FIXTURE_DATA_DIR)}`,
				'--host 127.0.0.1',
				`--port ${CMDK_PICK_RACE_API_PORT}`
			].join(' ')),
			cwd: REPOSITORY_ROOT,
			url: `${API_ORIGIN}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 120_000,
			// A stray MDT_DATA_DIR from a lane .env must not outrank this fixture dir,
			// and the daemon must know its paired frontend (request_guard.py, #2689).
			env: {
				...process.env,
				MDT_DATA_DIR: FIXTURE_DATA_DIR,
				MDT_LIBRARY_MODE: 'local',
				MDT_FEATURE_FLAGS_FILE: '',
				HOME: SANDBOX_HOME,
				WEB_CONCURRENCY: '',
				MUSIC_DJ_FRONTEND_PORT: String(CMDK_PICK_RACE_FRONTEND_PORT),
				MUSIC_DJ_BACKEND_PORT: String(CMDK_PICK_RACE_API_PORT)
			}
		},
		{
			command: guardedWebServerCommand('cmdk-pick-race-vite', `${shellArgument(PNPM)} exec vite --config tests/e2e/vite.cmdk-pick-race.config.ts`),
			cwd: FRONTEND_ROOT,
			url: `${FRONTEND_ORIGIN}/performance`,
			reuseExistingServer: false,
			timeout: 90_000
		}
	] }),
	use: {
		baseURL: FRONTEND_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'cmdk-pick-race-chromium', use: { ...devices['Desktop Chrome'] } }]
});
