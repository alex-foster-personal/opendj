/**
 * Browser smoke for the deck waveform's "Transition in around here" (AGENT-51).
 *
 * Real engine, real routers, real throwaway fixture library; the only seeded
 * dependency is one own beatgrid record so the route has downbeats to snap to
 * (support/transition_in_fixture.py says why and how).
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.transition-in-cue.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { guardedWebServerCommand } from './support/guarded-web-server';
import { TRANSITION_IN_CUE_API_PORT, TRANSITION_IN_CUE_FRONTEND_PORT } from './vite.transition-in-cue.config';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${TRANSITION_IN_CUE_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${TRANSITION_IN_CUE_API_PORT}`;
const FIXTURE_DATA_DIR = fileURLToPath(new URL('fixtures/transition-in-cue-data', import.meta.url));
// A fresh library every run, built in the main process before the engine
// starts (workers re-import this config beside a live engine, so never there).
if (process.env.TEST_WORKER_INDEX === undefined) rmSync(FIXTURE_DATA_DIR, { recursive: true, force: true });

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
	webServer: [
		{
			command: guardedWebServerCommand('transition-in-cue-engine', [
				`mkdir -p ${FIXTURE_DATA_DIR}`,
				'&&',
				'uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.transition_in_fixture',
				`--data-dir ${FIXTURE_DATA_DIR}`,
				'&&',
				'uv run --no-sync python -m apps.engine_core serve',
				`--data-dir ${FIXTURE_DATA_DIR}`,
				'--host 127.0.0.1',
				`--port ${TRANSITION_IN_CUE_API_PORT}`
			].join(' ')),
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
		},
		{
			command: guardedWebServerCommand(
				'transition-in-cue-vite',
				'pnpm exec vite --config tests/e2e/vite.transition-in-cue.config.ts'
			),
			cwd: FRONTEND_ROOT,
			url: `${FRONTEND_ORIGIN}/performance`,
			reuseExistingServer: false,
			timeout: 60_000
		}
	],
	use: { baseURL: FRONTEND_ORIGIN, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
	projects: [{ name: 'transition-in-cue-chromium', use: { ...devices['Desktop Chrome'] } }]
});
