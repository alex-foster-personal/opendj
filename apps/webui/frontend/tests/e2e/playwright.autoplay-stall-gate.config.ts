/**
 * Browser gate for PLAY-08's AutoPlay stall banner (issue #1640).
 *
 * WHY IT EXISTS. The banner's markup is asserted by an SSR render in
 * tests/unit/autoplay-stall-banner.test.mjs, and the controller that raises
 * and retires the stall is driven for real in
 * tests/unit/autoplay-stall-persistence.test.mjs. Neither can answer the
 * question the requirement is actually about: is the thing VISIBLE, does it
 * appear without a reload, does its toggle work, and does it obscure the decks
 * it is supposed to sit above (Codex r3973806306, r3974734057). Those are
 * layout and runtime questions and they need a browser.
 *
 * A fixture library IS needed, same as the comment-hotkey gate: the server's
 * own `/api/v1/preflight` `library-attached` check (track count > 0) holds
 * `overallStatus` at "fail" for a library-free data dir, which keeps
 * `/performance` on its "Starting up" screen forever - so the route that
 * mounts the banner never renders at all. Reuses the same real-audio,
 * real-ingest builder the other gates use rather than inventing a second one.
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.autoplay-stall-gate.config.ts
 *
 * Requirements:
 *   - [if] the banner renders but is not visible (clipped by the topbar's
 *     `overflow: hidden`, painted behind the waveform stack, zero height)
 *     [then] this fails where an SSR markup assertion cannot.
 *   - [if] the component stops reacting to a stall raised after mount [then]
 *     the visibility wait times out rather than silently passing.
 *   - [if] the expanded track list covers the toggle that closes it, or a deck
 *     control [then] the hittability assertions fail.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import {
	AUTOPLAY_STALL_GATE_API_PORT,
	AUTOPLAY_STALL_GATE_FRONTEND_PORT
} from './vite.autoplay-stall-gate.config';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${AUTOPLAY_STALL_GATE_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${AUTOPLAY_STALL_GATE_API_PORT}`;

const FIXTURE_DATA_DIR = fileURLToPath(
	new URL('fixtures/autoplay-stall-gate-data', import.meta.url)
);
const FIXTURE_BUILDER = fileURLToPath(new URL('support/deckload_fixture.py', import.meta.url));

export default defineConfig({
	testDir: '.',
	testMatch: 'autoplay-stall-browser.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	// The fixture builder's first run generates real audio and runs the real
	// ingest CLI, and the dev server pays a cold compile on the first
	// navigation. Same budget the other gates using this builder take.
	globalTimeout: 600_000,
	expect: { timeout: 10_000 },
	reporter: [['list']],
	webServer: [
		{
			command: guardedWebServerCommand('autoplay-stall-gate-engine', [
				`mkdir -p ${FIXTURE_DATA_DIR}`,
				'&&',
				// The library must exist before the backend opens it, and
				// Playwright starts webServers before globalSetup, which would
				// be too late. The builder is idempotent (FIXTURE_REVISION-gated).
				`uv run --no-sync python ${FIXTURE_BUILDER} --data-dir ${FIXTURE_DATA_DIR}`,
				'&&',
				'uv run --no-sync python -m apps.webui.server',
				'--host 127.0.0.1',
				`--port ${AUTOPLAY_STALL_GATE_API_PORT}`,
				'--prod'
			].join(' ')),
			cwd: REPOSITORY_ROOT,
			url: `${API_ORIGIN}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 180_000,
			// A stray MDT_DATA_DIR from a lane .env must not outrank this
			// fixture dir; see playwright.hotcue-mapping-gate.config.ts.
			env: {
				...process.env,
				MDT_DATA_DIR: FIXTURE_DATA_DIR,
				MDT_LIBRARY_MODE: 'local'
			}
		},
		{
			command: guardedWebServerCommand('autoplay-stall-gate-vite', 'pnpm exec vite --config tests/e2e/vite.autoplay-stall-gate.config.ts'),
			cwd: FRONTEND_ROOT,
			url: `${FRONTEND_ORIGIN}/performance`,
			reuseExistingServer: false,
			timeout: 60_000
		}
	],
	use: {
		baseURL: FRONTEND_ORIGIN,
		// The real-exhaustion test decodes and plays a real fixture track.
		launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'autoplay-stall-gate-chromium', use: { ...devices['Desktop Chrome'] } }]
});

export { AUTOPLAY_STALL_GATE_API_PORT, AUTOPLAY_STALL_GATE_FRONTEND_PORT };
