/**
 * End-to-end UI evidence for PREFLIGHT-01's boot gate (issue #771): a
 * healthy library lands straight in the app, and a library with nothing
 * imported yet holds the gate open with the real red row named -- proof the
 * gate actually RENDERS this way in a browser, which neither the backend
 * contract test (tests/webui/test_preflight.py) nor the frontend unit test
 * (tests/unit/preflight-gate.test.mjs) can show on their own.
 *
 * Two real backends, two real (throwaway) data dirs, on their own ports:
 *   - healthy: the same fixture builder the webkit/hotcue-mapping-gate
 *     suites use (real generated audio, real folder ingest), which by
 *     construction has readable file_path rows -> every check passes.
 *   - broken: a freshly initialized, empty data dir. The backend
 *     auto-migrates state.db to current schema on boot (so state-db passes)
 *     but there is nothing to import yet, which is the single most common
 *     real reason a user meets this gate -- library-attached fails, and
 *     audio-access is an honest `pending` (nothing recorded to even sample).
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.preflight-gate.config.ts
 *
 * Requirements:
 *   - OK Fixed loopback ports, never one already claimed by another lane/suite.
 *   - OK No retries: a gate that flakes past a real fail is not proof of anything.
 */
import { defineConfig, devices } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

export const PREFLIGHT_GATE_HEALTHY_API_PORT = 8696;
export const PREFLIGHT_GATE_HEALTHY_FRONTEND_PORT = 5321;
export const PREFLIGHT_GATE_BROKEN_API_PORT = 8697;
export const PREFLIGHT_GATE_BROKEN_FRONTEND_PORT = 5323;

export const PREFLIGHT_GATE_HEALTHY_ORIGIN = `http://127.0.0.1:${PREFLIGHT_GATE_HEALTHY_FRONTEND_PORT}`;
export const PREFLIGHT_GATE_BROKEN_ORIGIN = `http://127.0.0.1:${PREFLIGHT_GATE_BROKEN_FRONTEND_PORT}`;

const HEALTHY_DATA_DIR = fileURLToPath(
	new URL('fixtures/preflight-gate-healthy-data', import.meta.url)
);
const BROKEN_DATA_DIR = fileURLToPath(
	new URL('fixtures/preflight-gate-broken-data', import.meta.url)
);
mkdirSync(BROKEN_DATA_DIR, { recursive: true });

const FIXTURE_BUILDER = fileURLToPath(new URL('support/deckload_fixture.py', import.meta.url));

const VITE_CONFIG = fileURLToPath(new URL('vite.preflight-gate.config.ts', import.meta.url));

// The healthy library must exist before its backend opens it, and Playwright
// starts webServers before globalSetup -- same reasoning as
// playwright.hotcue-mapping-gate.config.ts.
const HEALTHY_BACKEND_COMMAND = [
	`uv run --no-sync python ${FIXTURE_BUILDER} --data-dir ${HEALTHY_DATA_DIR}`,
	'&&',
	'uv run --no-sync python -m apps.webui.server',
	'--host 127.0.0.1',
	`--port ${PREFLIGHT_GATE_HEALTHY_API_PORT}`,
	'--prod'
].join(' ');

// An empty data dir falls back to InMemoryBackend (no state.db means no
// SqliteBackend), which is a DIFFERENT failure mode than what this suite
// wants: a real migrated state.db with genuinely zero tracks (the ordinary
// "nothing imported yet" case, not "no database at all"). `state.cli init`
// is the real CLI this repo already ships for exactly that -- same tool
// deckload_fixture.py runs before it seeds tracks, just without the
// ingest-folder step that would follow it there.
function brokenBackendCommand(port: number, dataDir: string): string {
	return [
		'uv run --no-sync python -m apps.shared.state.cli init',
		'&&',
		'uv run --no-sync python -m apps.engine_core serve',
		`--data-dir ${dataDir}`,
		'--host 127.0.0.1',
		`--port ${port}`
	].join(' ');
}

function viteCommand(): string {
	return `pnpm exec vite --config ${VITE_CONFIG}`;
}

export default defineConfig({
	testDir: '.',
	// Anchored on `.spec.ts`. The bare alternation also matched THIS FILE
	// (testDir is '.'), so Playwright collected the config as a test, both
	// specs became "test file imports test file" errors, and the vite config
	// was loaded outside a webServer with no port env - three collection
	// errors that made the whole suite unrunnable rather than red.
	testMatch: /(preflight-gate|fresh-install-onboarding)\.spec\.ts$/,
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	globalTimeout: 300_000,
	expect: { timeout: 15_000 },
	reporter: [['list']],
	webServer: [
		{
			command: guardedWebServerCommand('preflight-gate-engine', HEALTHY_BACKEND_COMMAND),
			cwd: REPOSITORY_ROOT,
			url: `http://127.0.0.1:${PREFLIGHT_GATE_HEALTHY_API_PORT}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: { ...process.env, MDT_DATA_DIR: HEALTHY_DATA_DIR, MDT_LIBRARY_MODE: 'local' }
		},
		{
			command: guardedWebServerCommand('preflight-gate-vite', viteCommand()),
			cwd: FRONTEND_ROOT,
			url: `${PREFLIGHT_GATE_HEALTHY_ORIGIN}/`,
			reuseExistingServer: false,
			timeout: 60_000,
			env: {
				...process.env,
				PREFLIGHT_GATE_FRONTEND_PORT: String(PREFLIGHT_GATE_HEALTHY_FRONTEND_PORT),
				PREFLIGHT_GATE_API_PORT: String(PREFLIGHT_GATE_HEALTHY_API_PORT)
			}
		},
		{
			command: guardedWebServerCommand('preflight-gate-engine-2', brokenBackendCommand(PREFLIGHT_GATE_BROKEN_API_PORT, BROKEN_DATA_DIR)),
			cwd: REPOSITORY_ROOT,
			url: `http://127.0.0.1:${PREFLIGHT_GATE_BROKEN_API_PORT}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: { ...process.env, MDT_DATA_DIR: BROKEN_DATA_DIR, MDT_LIBRARY_MODE: 'local' }
		},
		{
			command: guardedWebServerCommand('preflight-gate-vite-2', viteCommand()),
			cwd: FRONTEND_ROOT,
			url: `${PREFLIGHT_GATE_BROKEN_ORIGIN}/`,
			reuseExistingServer: false,
			timeout: 60_000,
			env: {
				...process.env,
				PREFLIGHT_GATE_FRONTEND_PORT: String(PREFLIGHT_GATE_BROKEN_FRONTEND_PORT),
				PREFLIGHT_GATE_API_PORT: String(PREFLIGHT_GATE_BROKEN_API_PORT)
			}
		}
	],
	use: {
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'preflight-gate-chromium', use: { ...devices['Desktop Chrome'] } }]
});
