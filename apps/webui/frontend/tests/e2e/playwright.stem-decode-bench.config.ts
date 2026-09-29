/**
 * Stem-decode perf bench: four-way FLAC stem decode on the production path.
 *
 * MEASUREMENT lane, not a gate. Measures decodeStems from the deck-stems perf
 * ring after a real mix load against a throwaway demucs4 FLAC bundle fixture.
 *
 * Run::
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.stem-decode-bench.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

export const STEM_DECODE_BENCH_PORT = Number(process.env.STEM_DECODE_BENCH_PORT ?? 8700);

const RESERVED_PORTS: readonly number[] = [
	8585, 5173, 8685, 9405, 8682, 9402, 5216, 9473, 8688, 9408, 5273, 8686, 5399, 5311,
	8690, 8692, 8695, 8696, 8697, 8698, 8699, 8703, 8704
];

if (RESERVED_PORTS.includes(STEM_DECODE_BENCH_PORT)) {
	throw new Error(
		`stem-decode-bench port ${STEM_DECODE_BENCH_PORT} is claimed by another lane; pick a free one`
	);
}

export const STEM_DECODE_BENCH_ORIGIN = `http://127.0.0.1:${STEM_DECODE_BENCH_PORT}`;

export const FIXTURE_DATA_DIR = join(
	FRONTEND_ROOT,
	'tests',
	'e2e',
	'fixtures',
	'stem-decode-data'
);
export const FIXTURE_MANIFEST = join(FIXTURE_DATA_DIR, 'fixture-manifest.json');

const SANDBOX_HOME = join(FIXTURE_DATA_DIR, 'sandbox-home');
mkdirSync(SANDBOX_HOME, { recursive: true });

const BUILD_INDEX = join(FRONTEND_ROOT, 'build', 'index.html');
if (!existsSync(BUILD_INDEX)) {
	throw new Error(
		`stem-decode-bench needs the production build: ${BUILD_INDEX} does not exist. ` +
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
	'stem_decode_fixture.py'
);

const ENGINE_COMMAND = [
	`uv run --no-sync python ${FIXTURE_BUILDER}`,
	`--data-dir ${FIXTURE_DATA_DIR}`,
	`--manifest ${FIXTURE_MANIFEST}`,
	'&&',
	'uv run --no-sync python -m apps.engine_core serve',
	`--data-dir ${FIXTURE_DATA_DIR}`,
	'--host 127.0.0.1',
	`--port ${STEM_DECODE_BENCH_PORT}`
].join(' ');

export default defineConfig({
	testDir: '.',
	testMatch: ['stem-decode-bench.spec.ts'],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 600_000,
	globalTimeout: 1_200_000,
	expect: { timeout: 30_000 },
	reporter: [['list']],
	webServer: {
		command: guardedWebServerCommand('stem-decode-bench-engine', ENGINE_COMMAND),
		cwd: REPOSITORY_ROOT,
		url: `${STEM_DECODE_BENCH_ORIGIN}/api/v1/health`,
		reuseExistingServer: false,
		timeout: 240_000,
		env: {
			...process.env,
			MDT_DATA_DIR: FIXTURE_DATA_DIR,
			MDT_LIBRARY_MODE: 'local',
			WEB_CONCURRENCY: '',
			HOME: SANDBOX_HOME
		}
	},
	use: {
		baseURL: STEM_DECODE_BENCH_ORIGIN,
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
