/**
 * The onboarding gauntlet (SETUP-21..SETUP-23): a brand-new user's first run,
 * driven in a real browser against a real engine, plus the adversarial cases
 * that have broken it before. Spec and experiment log:
 * specs/onboarding-gauntlet.md.
 *
 * Modelled on playwright.preflight-gate.config.ts, with three deliberate
 * differences, each of which is the point:
 *
 *   - NO webServer. Every test boots its OWN engine on an OS-assigned port
 *     (support/onboarding-engine.ts), because every case starts from a truly
 *     empty library and most of them change it, and one case kills and
 *     relaunches the engine mid-import. A shared server could do neither.
 *   - the PACKAGED identity. The engine runs with OPENDJ_PAYLOAD_MANIFEST
 *     set, so GET /api/v1/setup/status says should_show_wizard and the
 *     wizard is opened by runFirstRunGate(), the path an installed app takes.
 *     The preflight-gate config runs a checkout engine, where only the
 *     empty-library fallback can open it.
 *   - the PRODUCTION build, served by the engine, as in the installed app.
 *
 * Run with (build first; this suite never starts vite):
 *
 *     pnpm build
 *     pnpm exec playwright test --config tests/e2e/playwright.onboarding.config.ts
 *
 * Requirements:
 *   - OK No fixed port: nothing here can collide with a lane or another suite.
 *   - OK One worker, no retries: a flaky first run is the defect under test.
 *   - OK Known open bugs are test.fail() with their issue number, never skipped.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

/** Machine-readable results, for the spec's experiment log and the PR evidence. */
const JSON_REPORT = fileURLToPath(new URL('../../test-results/onboarding-gauntlet.json', import.meta.url));

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls start-up (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	// Only this suite: the root config and others own every other spec here.
	testMatch: /onboarding-gauntlet-[\w-]+\.spec\.ts$/,
	fullyParallel: false,
	workers: 1,
	retries: 0,
	// Engine boot (13 s measured on a loaded Mac) + a small import + teardown.
	timeout: 240_000,
	globalTimeout: 1_500_000,
	expect: { timeout: 20_000 },
	reporter: [['list'], ['json', { outputFile: JSON_REPORT }]],
	use: {
		viewport: { width: 1600, height: 1000 },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
		video: 'off'
	},
	projects: [{ name: 'onboarding-chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1600, height: 1000 } } }]
});
