/**
 * PERF-UI-05 playlist tree and switch latency bench (issue #3530).
 *
 * Measures click-to-first-row paint on a real 1k-track fixture served by the
 * production build and the single-worker engine. This IS a gate: the spec
 * asserts p50 caps and writes measured samples for pytest to archive.
 *
 * Run::
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.playlist-switch-latency.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

export const PLAYLIST_SWITCH_BENCH_PORT = Number(
	process.env.PLAYLIST_SWITCH_BENCH_PORT ?? 8713
);

const RESERVED_PORTS: readonly number[] = [
	8585, 5173, 8685, 9405, 8682, 9402, 5216, 9473, 8688, 9408, 5273, 8686, 5399, 5311,
	8690, 8692, 8695, 8696, 8697, 8698, 8699, 8700, 8703, 8704
];

if (RESERVED_PORTS.includes(PLAYLIST_SWITCH_BENCH_PORT)) {
	throw new Error(
		`playlist-switch-latency port ${PLAYLIST_SWITCH_BENCH_PORT} is claimed by another lane`
	);
}

export const PLAYLIST_SWITCH_BENCH_ORIGIN = `http://127.0.0.1:${PLAYLIST_SWITCH_BENCH_PORT}`;

export const FIXTURE_DATA_DIR = join(
	FRONTEND_ROOT,
	'tests',
	'e2e',
	'fixtures',
	'playlist-switch-latency-data'
);

const SANDBOX_HOME = join(FIXTURE_DATA_DIR, 'sandbox-home');
mkdirSync(SANDBOX_HOME, { recursive: true });

const BUILD_INDEX = join(FRONTEND_ROOT, 'build', 'index.html');
if (!existsSync(BUILD_INDEX)) {
	throw new Error(
		`playlist-switch-latency needs the production build: ${BUILD_INDEX} does not exist. ` +
			'Run `pnpm build` in apps/webui/frontend first.'
	);
}

const FIXTURE_BUILDER = join(
	'apps',
	'webui',
	'frontend',
	'tests',
	'e2e',
	'support',
	'playlist_switch_fixture.py'
);

const ENGINE_COMMAND = [
	`uv run --no-sync python ${FIXTURE_BUILDER}`,
	`--data-dir ${FIXTURE_DATA_DIR}`,
	'&&',
	'uv run --no-sync python -m apps.engine_core serve',
	`--data-dir ${FIXTURE_DATA_DIR}`,
	'--host 127.0.0.1',
	`--port ${PLAYLIST_SWITCH_BENCH_PORT}`
].join(' ');

export default defineConfig({
	testDir: '.',
	testMatch: ['library-playlist-switch-latency.spec.ts'],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 600_000,
	globalTimeout: 1_200_000,
	expect: { timeout: 30_000 },
	reporter: [['list']],
	webServer: {
		command: guardedWebServerCommand('playlist-switch-latency-engine', ENGINE_COMMAND),
		cwd: REPOSITORY_ROOT,
		url: `${PLAYLIST_SWITCH_BENCH_ORIGIN}/api/v1/health`,
		reuseExistingServer: false,
		timeout: 240_000,
		env: {
			...process.env,
			MDT_DATA_DIR: FIXTURE_DATA_DIR,
			MDT_LIBRARY_MODE: 'local',
			WEB_CONCURRENCY: '',
			HOME: SANDBOX_HOME,
			// The fixture builder runs by path, so its own directory (not cwd) is
			// sys.path[0]; the CI shard venv is filled with `uv pip install -r
			// requirements.txt` and never installs the project, so `apps` is only
			// importable with the repository root on PYTHONPATH (#3741).
			PYTHONPATH: REPOSITORY_ROOT
		}
	},
	use: {
		baseURL: PLAYLIST_SWITCH_BENCH_ORIGIN,
		viewport: { width: 1280, height: 800 },
		trace: 'off',
		screenshot: 'off',
		video: 'off'
	},
	projects: [
		{
			name: 'chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } }
		}
	]
});
