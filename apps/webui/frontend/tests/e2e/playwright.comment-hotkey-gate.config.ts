/**
 * End-to-end UI evidence for the comment ('m') hotkey, against a REAL
 * backend (r3918992947): the prior browser spec set
 * `feedbackState.availability = 'ok'` directly, fabricating the exact state
 * the real /api/v1/feedback probe is responsible for establishing, so it
 * passed even if hydration, API compatibility, or production mounting never
 * makes the hotkey usable in the shipped app.
 *
 * A fixture library IS needed (added pin 18627f290052 / PR #1337): the
 * feedback routes themselves only ever touch <data-dir>/feedback/*.json, but
 * the server's own /api/v1/preflight `library-attached` check (track count >
 * 0, apps/webui/server/preflight_checks.py) holds `overallStatus` at "fail"
 * for a library-free data dir, which keeps /performance stuck on its
 * "Starting up" screen forever - so a throwaway EMPTY data dir cannot boot
 * this suite's own real `/performance` navigation at all, regardless of the
 * feedback routes' own needs. Reuses the same real-audio, real-ingest
 * fixture builder playwright.hotcue-mapping-gate.config.ts uses
 * (support/deckload_fixture.py) rather than inventing a second one, seeding
 * one throwaway library so double-clicking a track row (as
 * comment-hotkey-browser.spec.ts's waveform-seek-canvas test does) loads a
 * real track into deck 1.
 *
 * Only <data-dir>/feedback is wiped before each run, not the whole data
 * dir: the fixture builder is itself idempotent (FIXTURE_REVISION-gated, see
 * its own module docstring) and re-generating the audio + re-running the
 * real ingest on every run would needlessly slow this down, while stale
 * `.fb-pin`s from a PRIOR run stacking at identical coordinates is the
 * actual thing that must never survive between runs.
 *
 * Run with:
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.comment-hotkey-gate.config.ts
 *
 * Requirements:
 *   - [if] the real availability probe never resolves 'ok' [then] the test
 *     times out waiting for it, not silently proceeding on fabricated state.
 *   - [if] the real hotkey handler is unwired from the real /performance
 *     page [then] this fails where the fabricated-state version could not.
 *   - [if] the real /api/v1/preflight library-attached check still reports
 *     fail once the fixture library is seeded [then] /performance stays on
 *     "Starting up" and the test's own waits for feedback availability /
 *     the loaded track row time out, rather than silently proceeding
 *     against a stuck page.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import {
	COMMENT_HOTKEY_GATE_API_PORT,
	COMMENT_HOTKEY_GATE_FRONTEND_PORT
} from './vite.comment-hotkey-gate.config';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const FRONTEND_ORIGIN = `http://127.0.0.1:${COMMENT_HOTKEY_GATE_FRONTEND_PORT}`;
const API_ORIGIN = `http://127.0.0.1:${COMMENT_HOTKEY_GATE_API_PORT}`;

const FIXTURE_DATA_DIR = fileURLToPath(
	new URL('fixtures/comment-hotkey-gate-data', import.meta.url)
);
const FIXTURE_BUILDER = fileURLToPath(new URL('support/deckload_fixture.py', import.meta.url));

export default defineConfig({
	testDir: '.',
	testMatch: 'comment-hotkey-browser.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 30_000,
	// Widened from 180_000, then again from 300_000: the fixture builder's
	// first run generates real audio + runs the real ingest CLI (same cost
	// playwright.hotcue-mapping-gate.config.ts budgets for), and the file's
	// own beforeAll warm-up navigation (comment-hotkey-browser.spec.ts) adds
	// a real, generously-timed-out dev-server cold-compile wait on top of
	// all 13 tests' own runtime.
	globalTimeout: 600_000,
	expect: { timeout: 10_000 },
	reporter: [['list']],
	webServer: [
		{
			command: [
				// Only the feedback subtree is wiped before every run: pins from
				// a PRIOR run stay in <data-dir>/feedback/comments.json and stack
				// multiple `.fb-pin`s at identical coordinates, so a later run's
				// click can land on a stale pin instead of the one the test just
				// created. The fixture library itself is left alone so the
				// builder's own idempotency (FIXTURE_REVISION-gated) applies.
				`rm -rf ${FIXTURE_DATA_DIR}/feedback`,
				'&&',
				`mkdir -p ${FIXTURE_DATA_DIR}`,
				'&&',
				// Same ordering reason as playwright.hotcue-mapping-gate.config.ts:
				// the fixture library must exist before the backend opens it, and
				// Playwright starts webServers before globalSetup, which would be
				// too late.
				`uv run --no-sync python ${FIXTURE_BUILDER} --data-dir ${FIXTURE_DATA_DIR}`,
				'&&',
				'uv run --no-sync python -m apps.webui.server',
				'--host 127.0.0.1',
				`--port ${COMMENT_HOTKEY_GATE_API_PORT}`,
				'--prod'
			].join(' '),
			cwd: REPOSITORY_ROOT,
			url: `${API_ORIGIN}/api/v1/health`,
			reuseExistingServer: false,
			// Widened from 60_000: a cold fixture-audio generation + real ingest
			// pass (support/deckload_fixture.py) needs the same headroom
			// playwright.hotcue-mapping-gate.config.ts budgets for its own use of
			// the same builder.
			timeout: 120_000,
			// A stray MDT_DATA_DIR from a lane .env must not outrank this
			// fixture dir; see playwright.hotcue-mapping-gate.config.ts.
			//
			// MUSIC_DJ_FRONTEND_PORT / MUSIC_DJ_BACKEND_PORT tell the daemon
			// which frontend it is paired with. Since the mutating-origin guard
			// landed (apps/webui/server/request_guard.py, issue #2689) the
			// daemon 403s ORIGIN_NOT_ALLOWED on every POST/PUT/PATCH/DELETE
			// whose Origin is not its own paired frontend, and this suite's
			// backend was only ever told its own --port: it resolved the
			// frontend port from the checkout's root .env (whatever pair this
			// worktree happens to have claimed), never 5321, so every real pin
			// save was refused. Declaring the pairing is the same mechanism the
			// shipped dev server uses; it grants this suite's origin and
			// nothing else, so a foreign origin is still refused.
			env: {
				...process.env,
				MDT_DATA_DIR: FIXTURE_DATA_DIR,
				MDT_LIBRARY_MODE: 'local',
				MUSIC_DJ_FRONTEND_PORT: String(COMMENT_HOTKEY_GATE_FRONTEND_PORT),
				MUSIC_DJ_BACKEND_PORT: String(COMMENT_HOTKEY_GATE_API_PORT)
			}
		},
		{
			command: 'pnpm exec vite --config tests/e2e/vite.comment-hotkey-gate.config.ts',
			cwd: FRONTEND_ROOT,
			url: `${FRONTEND_ORIGIN}/performance`,
			reuseExistingServer: false,
			timeout: 90_000
		}
	],
	use: {
		baseURL: FRONTEND_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'comment-hotkey-gate-chromium', use: { ...devices['Desktop Chrome'] } }]
});

export { COMMENT_HOTKEY_GATE_API_PORT, COMMENT_HOTKEY_GATE_FRONTEND_PORT };
