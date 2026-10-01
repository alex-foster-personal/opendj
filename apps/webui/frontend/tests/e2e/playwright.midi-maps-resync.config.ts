/**
 * MIDI maps live-refresh resync gate (#1013): real engine, real WebSocket.
 *
 * Proves the installed device-map registry reloads after a real browser
 * disconnect/reconnect on /api/v1/events. The unit test
 * (midi-panel-resync.test.mjs) proves the same wiring through the documented
 * fake-socket seam; this lane closes the real-socket gap per ADR-0120.
 *
 * Run locally:
 *   pnpm exec playwright test --config tests/e2e/playwright.midi-maps-resync.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { resolveEndpoints, seedDataDir } from './midi-maps-e2e-endpoints';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

const endpoints = resolveEndpoints();
const dataDir = seedDataDir(REPOSITORY_ROOT);

process.env.MIDI_MAPS_E2E_API_BASE = endpoints.backendOrigin;
process.env.MIDI_MAPS_E2E_FRONTEND_PORT = String(endpoints.frontendPort);
process.env.MIDI_MAPS_E2E_DATA_DIR = dataDir;

// MUSIC_DJ_FRONTEND_PORT / MUSIC_DJ_BACKEND_PORT pair the daemon with this
// lane's frontend origin so mutating PUTs pass the origin guard (#2689).
const engineEnv = {
	...process.env,
	MDT_DATA_DIR: dataDir,
	MDT_LIBRARY_MODE: 'local',
	MUSIC_DJ_FRONTEND_PORT: String(endpoints.frontendPort),
	MUSIC_DJ_BACKEND_PORT: String(endpoints.backendPort)
};

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'midi-maps-resync.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 90_000,
	globalTimeout: 300_000,
	expect: { timeout: 20_000 },
	reporter: [['list']],
	webServer: [
		{
			command: guardedWebServerCommand('midi-maps-resync-engine', `uv run --no-sync python -m apps.engine_core serve --data-dir ${dataDir} --host 127.0.0.1 --port ${endpoints.backendPort}`),
			cwd: REPOSITORY_ROOT,
			url: `${endpoints.backendOrigin}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: engineEnv
		},
		{
			command: guardedWebServerCommand('midi-maps-resync-vite', 'pnpm exec vite --config tests/e2e/vite.midi-maps.config.ts'),
			cwd: FRONTEND_ROOT,
			url: `${endpoints.frontendOrigin}/performance`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: {
				...process.env,
				MIDI_MAPS_E2E_API_BASE: endpoints.backendOrigin,
				MIDI_MAPS_E2E_FRONTEND_PORT: String(endpoints.frontendPort)
			}
		}
	],
	use: {
		baseURL: endpoints.frontendOrigin,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'midi-maps-resync-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } }
		}
	]
});
