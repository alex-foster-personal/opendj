/**
 * Cmd K cue-preview gate (CMDK-07..08; Codex r4278602487 on #6687): the bar's
 * preview strip and its arrow-key cue preview driven against the real engine,
 * the real decoder, the real typed dispatcher (`preview_cue` / `preview_stop`)
 * and a real fixture library, with `cmdk.v2` and `cmdk.v2.preview` on through
 * the data dir's own feature-flags.json. The unit suite's recording harness
 * stays; this is the production path it cannot reach.
 *
 * The fixture (support/cmdk_preview_cue_fixture.py) is rebuilt on every run:
 * generated audio, the real folder ingest, then the production librosa
 * analyzer. `/anlz` intentionally stays gridless for these unmapped local
 * tracks, while `/beatgrid-fallback` serves the measured downbeats the bar
 * shares with the deck waveform. The builder fails loudly without those
 * downbeats, so the gate never runs gridless.
 *
 * The engine starts through `guardedWebServerCommand`, which every e2e config
 * here shares and which launches `/bin/sh` and `.venv/bin/python`: POSIX-only
 * today. So on Windows this config starts no server and the spec skips, naming
 * that dependency (AGENTS.md "Portable by default"), rather than failing at
 * startup.
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.cmdk-preview-cue.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { CMDK_PREVIEW_CUE_API_PORT, CMDK_PREVIEW_CUE_FRONTEND_PORT } from './vite.cmdk-preview-cue.config';
import { guardedWebServerCommand, shellArgument } from './support/guarded-web-server';
import { resolveProgram } from './support/resolve-program';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${CMDK_PREVIEW_CUE_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${CMDK_PREVIEW_CUE_API_PORT}`;
const FIXTURE_DATA_DIR = fileURLToPath(new URL('fixtures/cmdk-preview-cue-data', import.meta.url));
const SANDBOX_HOME = `${FIXTURE_DATA_DIR}-home`;
/** The shared launcher (`guardedWebServerCommand`) is POSIX-only: see the header. */
const NO_POSIX_LAUNCHER = process.platform === 'win32';
const FIXTURE_BUILDER_MODULE = 'apps.webui.frontend.tests.e2e.support.cmdk_preview_cue_fixture';
/** uv, resolved once: the gate launches this path in every step, not a bare name the PATH finds later (Codex P1, #6689). */
const UV = NO_POSIX_LAUNCHER ? 'uv' : resolveProgram('uv');
/** pnpm, resolved once for the Vite server step (Codex P1, #6689). */
const PNPM = NO_POSIX_LAUNCHER ? 'pnpm' : resolveProgram('pnpm');

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: ['cmdk-preview-cue.spec.ts'],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 120_000,
	globalTimeout: 600_000,
	expect: { timeout: 15_000 },
	reporter: [['list']],
	...(NO_POSIX_LAUNCHER ? {} : { webServer: [
		{
			command: guardedWebServerCommand('cmdk-preview-cue-engine', [
				// The fixture builder (Python) also creates the sandbox HOME: no shell-specific step here.
				`${shellArgument(UV)} run --no-sync python -m ${FIXTURE_BUILDER_MODULE} --data-dir ${shellArgument(FIXTURE_DATA_DIR)} --home ${shellArgument(SANDBOX_HOME)}`,
				'&&',
				// The engine, not apps.webui.server: it serves /api/v1/flags, which turns the v2 bar on.
				`${shellArgument(UV)} run --no-sync python -m apps.engine_core serve --data-dir ${shellArgument(FIXTURE_DATA_DIR)}`,
				'--host 127.0.0.1',
				`--port ${CMDK_PREVIEW_CUE_API_PORT}`
			].join(' ')),
			cwd: REPOSITORY_ROOT,
			url: `${API_ORIGIN}/api/v1/health`,
			reuseExistingServer: false,
			// The fixture ingests and analyzes two generated tracks before the engine starts.
			timeout: 180_000,
			// A stray MDT_DATA_DIR from a lane .env must not outrank this fixture dir,
			// and the daemon must know its paired frontend (request_guard.py, #2689).
			env: {
				...process.env,
				MDT_DATA_DIR: FIXTURE_DATA_DIR,
				MDT_LIBRARY_MODE: 'local',
				MDT_FEATURE_FLAGS_FILE: '',
				HOME: SANDBOX_HOME,
				WEB_CONCURRENCY: '',
				MUSIC_DJ_FRONTEND_PORT: String(CMDK_PREVIEW_CUE_FRONTEND_PORT),
				MUSIC_DJ_BACKEND_PORT: String(CMDK_PREVIEW_CUE_API_PORT)
			}
		},
		{
			command: guardedWebServerCommand('cmdk-preview-cue-vite', `${shellArgument(PNPM)} exec vite --config tests/e2e/vite.cmdk-preview-cue.config.ts`),
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
	projects: [{ name: 'cmdk-preview-cue-chromium', use: { ...devices['Desktop Chrome'] } }]
});
