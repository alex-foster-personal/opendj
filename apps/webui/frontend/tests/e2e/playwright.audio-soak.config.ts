/**
 * The audio endurance soak: its own tier, because it is measured in MINUTES.
 *
 * A SEPARATE CONFIG IS THE TIERING. This repo has no Playwright tag or grep
 * mechanism -- every suite that must not run in the fast lane gets its own
 * config and its own entry point, and the root `playwright.config.ts`
 * `testIgnore`s the spec. That is how `meter-artifact`, `stretch-artifact` and
 * `webkit-deckload` are each kept off the PR gate, and this follows it exactly.
 * Nothing here is reachable from `pnpm test:e2e`.
 *
 * Run with::
 *
 *     just test-audio-soak            # 10 minutes, the default
 *     just test-audio-soak 25         # 25 minutes
 *     MDT_AUDIO_SOAK_MINUTES=2 pnpm exec playwright test \
 *         --config tests/e2e/playwright.audio-soak.config.ts
 *
 * Served from the BUILT artifact for the same reason the meter gate next door
 * is: the harness loads the real `meter-processor.js` worklet through Vite's
 * `?url` emit, and only the built output proves that asset resolves.
 *
 * Requirements (mini-PRD):
 *
 * - ok Never run in the fast lane.
 *     [if] the root config stops ignoring `audio-soak.spec.ts` [then STOP] a
 *     ten-minute suite joins the PR gate.
 * - ok Own a fixed port outside every reserved pair.
 *     [if] the port is one another lane owns [then STOP] the module loads.
 * - ok Give the soak more wall clock than it asks for.
 *     [if] the Playwright timeout is below the soak duration [then STOP] the
 *     run is killed mid-soak and reported as a timeout rather than a verdict.
 * - ok Allow the schedule to finish one full pass past that duration.
 *     [if] the timeout does not cover `SOAK_OVERRUN_SLACK_MS` [then STOP] the
 *     two-minute invocation is killed part way through its first pass, which
 *     is the one thing the duration is NOT allowed to shorten.
 *
 * Acceptance tests:
 *
 * - [if] the port collides with a live lane service [then STOP] the run starts.
 * - [if] a server is already listening on this port [then STOP] the suite
 *   attaches to it, because it would then measure somebody else's build.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

/** This suite's own port, checked free at authoring time. */
export const AUDIO_SOAK_PORT = 5324;

/** Ports the primary checkout, live lane services and other suites already own. */
const RESERVED = new Set([
	8585, 5173, 8685, 9405, 8682, 9402, 5216, 9473, 8688, 9408, 5273, 8686,
	5399, 5311, 5320, 5214, 8690, 8692, 8695, 5322
]);

if (RESERVED.has(AUDIO_SOAK_PORT)) {
	throw new Error(
		`audio-soak port ${AUDIO_SOAK_PORT} is owned by another lane or suite; pick another`
	);
}

export const AUDIO_SOAK_ORIGIN = `http://127.0.0.1:${AUDIO_SOAK_PORT}`;
export const AUDIO_SOAK_BUILD_DIR = `${FRONTEND_ROOT}build`;

/**
 * How long the soak plays for. Parameterised because the SAME suite is both the
 * quick "does it still bite" check (2 minutes) and the endurance run (10+).
 */
export function soakMinutes(): number {
	const raw = process.env.MDT_AUDIO_SOAK_MINUTES ?? '10';
	const minutes = Number(raw);
	if (!Number.isFinite(minutes) || minutes <= 0) {
		throw new Error(
			`MDT_AUDIO_SOAK_MINUTES must be a positive number, got ${JSON.stringify(raw)}`
		);
	}
	return minutes;
}

export const SOAK_DURATION_MS = soakMinutes() * 60_000;

/**
 * How far past the requested duration a run is allowed to go, because the
 * duration is a floor on SOAKING and not a budget the hostile schedule may be
 * truncated to fit (Sol P1 BLOCKING, PR #1644).
 *
 * The harness runs every scheduled event at least once whatever duration was
 * asked for. Events sit three rebind cooldowns apart, so one full pass of the
 * seven-event schedule costs roughly four minutes and a two-minute run
 * deliberately overruns. Five minutes covers that pass with room to spare;
 * without it the quick invocation would stop after two events while the suite
 * asserted a verdict on the five that never ran.
 */
export const SOAK_OVERRUN_SLACK_MS = 300_000;

/**
 * The soak, plus its overrun, plus the control tests, plus build and teardown
 * slack. A timeout that merely equalled the soak would turn a healthy run into
 * a timeout, and one that ignored the overrun would kill every short run.
 */
const TEST_TIMEOUT_MS = SOAK_DURATION_MS + SOAK_OVERRUN_SLACK_MS + 120_000;

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'audio-soak.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: TEST_TIMEOUT_MS,
	globalTimeout: TEST_TIMEOUT_MS * 2 + 600_000,
	expect: { timeout: 30_000 },
	reporter: [['list']],
	webServer: {
		command: guardedWebServerCommand('audio-soak-static', `pnpm build && python3 -m http.server ${AUDIO_SOAK_PORT} --bind 127.0.0.1 --directory build`),
		cwd: FRONTEND_ROOT,
		url: `${AUDIO_SOAK_ORIGIN}/index.html`,
		reuseExistingServer: false,
		timeout: 300_000
	},
	use: {
		baseURL: AUDIO_SOAK_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'audio-soak-chromium',
			use: {
				...devices['Desktop Chrome'],
				launchOptions: {
					// A soak with no audio measures nothing, and this suite must not
					// depend on a user gesture to get a running context.
					args: ['--autoplay-policy=no-user-gesture-required']
				}
			}
		}
	]
});
