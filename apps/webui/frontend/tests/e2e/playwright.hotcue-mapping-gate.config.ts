/**
 * End-to-end UI evidence that a deck with no rekordbox mapping saves hot cues
 * into Open DJ's own cue store (CUES-01; it replaced the #736 inert gate), and
 * that a deck with nothing loaded stays inert (#804).
 *
 * The unit suites pin the source text and the dispatcher; neither proves a
 * real click reaches the server and the pad renders filled. This is that
 * proof, against a real backend and a real (throwaway) library.
 *
 * Library: the same generator deckload_fixture.py uses for the webkit suite
 * (support/deckload_fixture.py) -- real audio, real folder ingest, so both
 * fixture tracks carry no rekordbox vendor mapping by construction. Its own
 * disposable data dir (the engine takes a singleton lock on one), never
 * shared with webkit-deckload's or boot-burst's.
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.hotcue-mapping-gate.config.ts
 *
 * Requirements:
 *   - ✔︎ Fixed loopback ports, never one already claimed by another lane/suite.
 *   - ✔︎ No retries, so a pad that flakes into life is not hidden.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { HOTCUE_MAPPING_GATE_API_PORT, HOTCUE_MAPPING_GATE_FRONTEND_PORT } from './vite.hotcue-mapping-gate.config';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${HOTCUE_MAPPING_GATE_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${HOTCUE_MAPPING_GATE_API_PORT}`;

const FIXTURE_DATA_DIR = fileURLToPath(
	new URL('fixtures/hotcue-mapping-gate-data', import.meta.url)
);
const FIXTURE_BUILDER = fileURLToPath(new URL('support/deckload_fixture.py', import.meta.url));

// One shell command, two ordered steps, same reasoning as
// playwright.webkit-deckload.config.ts: the fixture library must exist
// before the backend opens it, and Playwright starts webServers before
// globalSetup, which would be too late.
const BACKEND_COMMAND = [
	`uv run --no-sync python ${FIXTURE_BUILDER} --data-dir ${FIXTURE_DATA_DIR}`,
	'&&',
	'uv run --no-sync python -m apps.webui.server',
	`--host 127.0.0.1`,
	`--port ${HOTCUE_MAPPING_GATE_API_PORT}`,
	'--prod'
].join(' ');

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'hot-cue-mapping-gate.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	globalTimeout: 300_000,
	expect: { timeout: 15_000 },
	reporter: [['list']],
	webServer: [
		{
			command: guardedWebServerCommand('hotcue-mapping-gate-engine', BACKEND_COMMAND),
			cwd: REPOSITORY_ROOT,
			url: `${API_ORIGIN}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 120_000,
			// A stray MDT_DATA_DIR from a lane .env must not outrank this fixture
			// dir. MDT_LIBRARY_MODE is required outside darwin
			// (apps/shared/library_mode.py). WEB_CONCURRENCY is deliberately left
			// untouched, not blanked: uvicorn parses it with int(), so an empty
			// string breaks startup where an absent key does not.
			env: {
				...process.env,
				MDT_DATA_DIR: FIXTURE_DATA_DIR,
				MDT_LIBRARY_MODE: 'local',
				MUSIC_DJ_FRONTEND_PORT: String(HOTCUE_MAPPING_GATE_FRONTEND_PORT)
			}
		},
		{
			command: guardedWebServerCommand('hotcue-mapping-gate-vite', 'pnpm exec vite --config tests/e2e/vite.hotcue-mapping-gate.config.ts'),
			cwd: FRONTEND_ROOT,
			url: `${FRONTEND_ORIGIN}/performance`,
			reuseExistingServer: false,
			timeout: 60_000
		}
	],
	use: {
		baseURL: FRONTEND_ORIGIN,
		viewport: { width: 1600, height: 1000 },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'hotcue-mapping-gate-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1600, height: 1000 } }
		}
	]
});

export { HOTCUE_MAPPING_GATE_API_PORT, HOTCUE_MAPPING_GATE_FRONTEND_PORT };
