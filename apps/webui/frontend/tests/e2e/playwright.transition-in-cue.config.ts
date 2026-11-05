/**
 * Browser smoke for the deck waveform's "Transition in around here" (AGENT-51).
 *
 * Real engine, real routers, real throwaway fixture library; the only seeded
 * dependency is one own beatgrid record so the route has downbeats to snap to
 * (support/transition_in_fixture.py says why and how).
 *
 * Engine startup is one Python launcher (transition_in_e2e_server.py): seed the
 * fixture library in a child, then serve through engine_core in-process. The
 * shared guard (`guardedWebServerCommand`) is still POSIX (`/bin/sh`,
 * `.venv/bin/python`, process groups); on Windows this config starts no server.
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.transition-in-cue.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { guardedWebServerCommand } from './support/guarded-web-server';
import { resolveProgram } from './support/resolve-program';
import {
	TRANSITION_IN_CUE_API_PORT,
	TRANSITION_IN_CUE_FRONTEND_PORT
} from './vite.transition-in-cue.config';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${TRANSITION_IN_CUE_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${TRANSITION_IN_CUE_API_PORT}`;
const FIXTURE_DATA_DIR = fileURLToPath(new URL('fixtures/transition-in-cue-data', import.meta.url));
/** The shared launcher (`guardedWebServerCommand`) is POSIX-only: see the header. */
const NO_POSIX_LAUNCHER = process.platform === 'win32';
const UV = NO_POSIX_LAUNCHER ? 'uv' : resolveProgram('uv');
// A fresh library every run, built in the main process before the engine
// starts (workers re-import this config beside a live engine, so never there).
if (process.env.TEST_WORKER_INDEX === undefined)
	rmSync(FIXTURE_DATA_DIR, { recursive: true, force: true });

const engine = {
	command: guardedWebServerCommand('transition-in-cue-engine', [
		UV,
		'run',
		'--no-sync',
		'python',
		'-m',
		'apps.webui.frontend.tests.e2e.support.transition_in_e2e_server',
		'--data-dir',
		FIXTURE_DATA_DIR,
		'--host',
		'127.0.0.1',
		'--port',
		String(TRANSITION_IN_CUE_API_PORT)
	]),
	cwd: REPOSITORY_ROOT,
	url: `${API_ORIGIN}/api/v1/health`,
	reuseExistingServer: false,
	timeout: 180_000,
	// A stray MDT_DATA_DIR from a lane .env must not outrank this fixture dir.
	// MUSIC_DJ_FRONTEND_PORT makes the engine trust this gate's vite origin for writes.
	env: {
		...process.env,
		MDT_DATA_DIR: FIXTURE_DATA_DIR,
		MDT_LIBRARY_MODE: 'local',
		MUSIC_DJ_FRONTEND_PORT: String(TRANSITION_IN_CUE_FRONTEND_PORT)
	}
};

const vite = {
	command: guardedWebServerCommand(
		'transition-in-cue-vite',
		'pnpm exec vite --config tests/e2e/vite.transition-in-cue.config.ts'
	),
	cwd: FRONTEND_ROOT,
	url: `${FRONTEND_ORIGIN}/performance`,
	reuseExistingServer: false,
	timeout: 60_000
};

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: ['transition-in-cue.spec.ts'],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	globalTimeout: 600_000,
	expect: { timeout: 10_000 },
	reporter: [['list']],
	...(NO_POSIX_LAUNCHER ? {} : { webServer: [engine, vite] }),
	use: { baseURL: FRONTEND_ORIGIN, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
	projects: [{ name: 'transition-in-cue-chromium', use: { ...devices['Desktop Chrome'] } }]
});
