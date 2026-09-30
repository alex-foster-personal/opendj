/**
 * AutoPlay / mixing error hunt: its own tier, because it is measured in MINUTES.
 *
 * A SEPARATE CONFIG IS THE TIERING. This repo has no Playwright tag or grep
 * mechanism -- every suite that must not run in the fast lane gets its own
 * config and its own entry point, and the root `playwright.config.ts`
 * `testIgnore`s the spec. Nothing here is reachable from `pnpm test:e2e`.
 *
 * Unlike `audio-soak.spec.ts` (modelled output, no /performance), this boots
 * the REAL engine (`apps.engine_core serve`) and the real `/performance` UI
 * against a 6-track throwaway fixture, starts AutoPlay, interleaves seeded
 * mixing, and fails on every error signature not named in
 * `autoplay-error-hunt.allow.json`.
 *
 * Run with::
 *
 *     just test-autoplay-hunt              # 20 minutes, the default
 *     just test-autoplay-hunt 2            # green check on the Linux chromium e2e host
 *     MDT_AUTOPLAY_HUNT_MINUTES=2 pnpm exec playwright test \
 *         --config tests/e2e/playwright.autoplay-error-hunt.config.ts
 *
 * Feeds #1778 / #1853.
 */
import { defineConfig, devices } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
	AUTOPLAY_HUNT_API_PORT,
	AUTOPLAY_HUNT_FRONTEND_PORT
} from './vite.autoplay-error-hunt.config';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${AUTOPLAY_HUNT_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${AUTOPLAY_HUNT_API_PORT}`;

/** Ports the primary checkout, live lane services and other suites already own. */
const RESERVED = new Set([
	4455, 4456, 5214, 5216, 5273, 5311, 5320, 5321, 5322, 5323, 5324, 5399, 5173,
	8585, 8682, 8685, 8686, 8688, 8690, 8691, 8692, 8695, 8696, 8697, 8698, 8699,
	9402, 9405, 9408, 9414, 9473
]);

if (RESERVED.has(AUTOPLAY_HUNT_FRONTEND_PORT) || RESERVED.has(AUTOPLAY_HUNT_API_PORT)) {
	throw new Error(
		`autoplay-error-hunt ports ${AUTOPLAY_HUNT_FRONTEND_PORT}/${AUTOPLAY_HUNT_API_PORT} ` +
			'are owned by another lane or suite; pick another'
	);
}

export const FIXTURE_DATA_DIR = join(
	FRONTEND_ROOT,
	'tests',
	'e2e',
	'fixtures',
	'autoplay-error-hunt-data'
);
export const FIXTURE_MANIFEST_PATH = join(FIXTURE_DATA_DIR, 'fixture-manifest.json');

/**
 * HOME for the engine under test. A fixture engine that keeps the real HOME
 * can see the real rekordbox library (webkit-deckload: Wed 19 Aug 2026).
 */
const SANDBOX_HOME = join(FIXTURE_DATA_DIR, 'sandbox-home');
mkdirSync(SANDBOX_HOME, { recursive: true });

export const HUNT_HTML_REPORT_DIR = join(FRONTEND_ROOT, 'playwright-report/autoplay-error-hunt');
export const HUNT_OUTPUT_DIR = join(FRONTEND_ROOT, 'test-results/autoplay-error-hunt');

/**
 * How long the hunt mixes for. Parameterised because the SAME suite is both the
 * quick "does it still bite" check (2 minutes) and the endurance run (20).
 */
export function huntMinutes(): number {
	const raw = process.env.MDT_AUTOPLAY_HUNT_MINUTES ?? '20';
	const minutes = Number(raw);
	if (!Number.isFinite(minutes) || minutes <= 0) {
		throw new Error(
			`MDT_AUTOPLAY_HUNT_MINUTES must be a positive number, got ${JSON.stringify(raw)}`
		);
	}
	return minutes;
}

export function huntSeed(): number {
	const raw = process.env.MDT_AUTOPLAY_HUNT_SEED ?? '1853';
	const seed = Number(raw);
	if (!Number.isFinite(seed)) {
		throw new Error(`MDT_AUTOPLAY_HUNT_SEED must be a number, got ${JSON.stringify(raw)}`);
	}
	return seed;
}

export const HUNT_DURATION_MS = huntMinutes() * 60_000;

/** Mixer + playlist switch + negative-control slack past the requested duration. */
const TEST_TIMEOUT_MS = HUNT_DURATION_MS + 180_000;

function shellArgument(value: string): string {
	return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

const FIXTURE_COMMAND = [
	'uv',
	'run',
	'--no-sync',
	'python',
	'-m',
	'apps.webui.frontend.tests.e2e.support.deckload_fixture',
	'--data-dir',
	FIXTURE_DATA_DIR,
	'--seed-autoplay-hunt',
	'--manifest',
	FIXTURE_MANIFEST_PATH
]
	.map(shellArgument)
	.join(' ');
const ENGINE_COMMAND = [
	'uv',
	'run',
	'--no-sync',
	'python',
	'-m',
	'apps.engine_core',
	'serve',
	'--data-dir',
	FIXTURE_DATA_DIR,
	'--host',
	'127.0.0.1',
	'--port',
	String(AUTOPLAY_HUNT_API_PORT)
]
	.map(shellArgument)
	.join(' ');
const SERVER_COMMAND = `${FIXTURE_COMMAND} && ${ENGINE_COMMAND}`;

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'autoplay-error-hunt.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: TEST_TIMEOUT_MS,
	// 6 generated 60 s wavs plus ingest is the slow part of first boot.
	globalTimeout: TEST_TIMEOUT_MS * 2 + 180_000,
	expect: { timeout: 30_000 },
	reporter: [['list'], ['html', { outputFolder: HUNT_HTML_REPORT_DIR, open: 'never' }]],
	outputDir: HUNT_OUTPUT_DIR,
	webServer: [
		{
			command: guardedWebServerCommand('autoplay-error-hunt-engine', SERVER_COMMAND),
			cwd: REPOSITORY_ROOT,
			url: `${API_ORIGIN}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 180_000,
			env: {
				...process.env,
				MDT_DATA_DIR: FIXTURE_DATA_DIR,
				MDT_LIBRARY_MODE: 'local',
				WEB_CONCURRENCY: '',
				HOME: SANDBOX_HOME
			}
		},
		{
			command: guardedWebServerCommand('autoplay-error-hunt-vite', 'pnpm exec vite --config tests/e2e/vite.autoplay-error-hunt.config.ts'),
			cwd: FRONTEND_ROOT,
			url: `${FRONTEND_ORIGIN}/performance`,
			reuseExistingServer: false,
			timeout: 90_000
		}
	],
	use: {
		baseURL: FRONTEND_ORIGIN,
		viewport: { width: 1600, height: 1200 },
		launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'autoplay-error-hunt-chromium', use: { ...devices['Desktop Chrome'] } }]
});

export { AUTOPLAY_HUNT_API_PORT, AUTOPLAY_HUNT_FRONTEND_PORT };
