/**
 * SETUP-21: a brand-new user's first run, end to end, on a packaged build.
 *
 * The user installs the app, opens it, and has music in a folder and no
 * rekordbox. Nothing is pre-seeded: the data dir is empty, HOME is a sandbox,
 * and the engine identifies as an installed build (support/onboarding-engine).
 *
 * Single-line acceptance checks:
 * - if the wizard does not open by itself within TIME_TO_WIZARD_BUDGET_MS -> broken.
 * - if the engine's own status does not ask for the wizard (should_show_wizard) -> broken.
 * - if a folder import of generated audio does not reach `succeeded` -> broken.
 * - if All Tracks does not list every imported track without a reload -> broken.
 * - if the Done screen does not report the imported count -> broken (#3422, test.fail).
 * - if "Start playing" on the Done screen does not close the wizard -> broken
 *   (#3422, test.fail: the same skip-then-close path that bounces "Skip for now").
 * - if a first-run screen shows a new user an endpoint path, an error code or
 *   an env-var name -> broken (#2590, test.fail).
 */
import { expect, test } from '@playwright/test';

import { type OnboardingEngine, startOnboardingEngine, writeMusicFolder } from './support/onboarding-engine';
import {
	TIME_TO_WIZARD_BUDGET_MS,
	allTracksRowCount,
	checkFolder,
	chooseFolderBranch,
	continueToDone,
	landAndTimeWizard,
	minimiseWizard,
	readSetupStatus,
	setupDialog,
	startFolderImport,
	waitForTerminalJob
} from './support/onboarding-wizard';

const TRACKS = 4;

/** "Start playing" runs the same skip-then-close as "Skip for now", so it meets
 * #3422's bounce: found by this suite's round 0 (Thu 1 Oct 2026), see
 * specs/onboarding-gauntlet.md. */
const START_PLAYING_ISSUE = '#3422';

let engine: OnboardingEngine;

test.beforeEach(async ({}, testInfo) => {
	engine = await startOnboardingEngine(testInfo.title.replace(/\W+/g, '-').slice(0, 40));
});

test.afterEach(async ({}, testInfo) => {
	if (testInfo.status !== testInfo.expectedStatus) {
		await testInfo.attach('engine.log', { body: engine.logTail(), contentType: 'text/plain' });
	}
	await engine.dispose();
});

test.describe('onboarding gauntlet: happy path', () => {
	test('a new user lands, the wizard opens itself, and a folder import fills All Tracks', async ({ page }, testInfo) => {
		const music = `${engine.musicRoot}/My Music`;
		writeMusicFolder(music, TRACKS);

		const before = await readSetupStatus(engine.origin);
		expect(before).toMatchObject({ library_empty: true, dev_mode: false, should_show_wizard: true });

		const statusCalls: string[] = [];
		page.on('request', (request) => {
			if (request.url().includes('/api/v1/setup/status')) statusCalls.push(request.url());
		});
		const timeToWizardMs = await landAndTimeWizard(page, engine.origin);
		testInfo.annotations.push({ type: 'time-to-wizard-ms', description: String(timeToWizardMs) });
		expect(statusCalls.length, 'the packaged first-run gate never asked GET /api/v1/setup/status').toBeGreaterThan(0);
		expect(timeToWizardMs, `wizard took ${timeToWizardMs} ms`).toBeLessThan(TIME_TO_WIZARD_BUDGET_MS);

		await chooseFolderBranch(page);
		const scan = await checkFolder(page, music);
		expect(scan).toMatchObject({ readable: true, denied: false, audio_files: TRACKS });
		await expect(setupDialog(page).getByText(`${TRACKS} audio files found.`)).toBeVisible();

		const importStarted = Date.now();
		const jobId = await startFolderImport(page);
		const job = await waitForTerminalJob(engine.origin, jobId);
		testInfo.annotations.push({ type: 'import-ms', description: String(Date.now() - importStarted) });
		expect(job.status, JSON.stringify(job)).toBe('succeeded');

		await continueToDone(page);
		// Minimise, not "Start playing": that button is its own case below,
		// and this one is about the library the import filled.
		await minimiseWizard(page);

		// No reload anywhere above: the rows must arrive in the running page.
		await allTracksRowCount(page, TRACKS);
		const after = await readSetupStatus(engine.origin);
		expect(after).toMatchObject({ library_empty: false, tracks: TRACKS, should_show_wizard: false });
	});

	test('the Done screen reports how many tracks the import wrote (#3422)', async ({ page }) => {
		test.fail(true, 'issue #3422: Done reads last_import only when the overlay opens, so it says no import was recorded');
		const music = `${engine.musicRoot}/My Music`;
		writeMusicFolder(music, TRACKS);
		await landAndTimeWizard(page, engine.origin);
		await chooseFolderBranch(page);
		await checkFolder(page, music);
		const jobId = await startFolderImport(page);
		expect((await waitForTerminalJob(engine.origin, jobId)).status).toBe('succeeded');

		const done = await continueToDone(page);
		const status = await readSetupStatus(engine.origin);
		expect(status.last_import?.tracks_written).toBe(TRACKS);
		await expect(done.getByText('No import was recorded for this data directory')).toHaveCount(0);
		await expect(done.getByText(`${TRACKS} tracks`, { exact: true })).toBeVisible();
	});

	test('"Start playing" on the Done screen closes the wizard and leaves it closed', async ({ page }) => {
		test.fail(true, `issue ${START_PLAYING_ISSUE}: the wizard stays open after a successful first import`);
		const music = `${engine.musicRoot}/My Music`;
		writeMusicFolder(music, TRACKS);
		await landAndTimeWizard(page, engine.origin);
		await chooseFolderBranch(page);
		await checkFolder(page, music);
		const jobId = await startFolderImport(page);
		expect((await waitForTerminalJob(engine.origin, jobId)).status).toBe('succeeded');

		const done = await continueToDone(page);
		await done.getByRole('button', { name: 'Start playing' }).click();
		await expect(setupDialog(page)).toHaveCount(0, { timeout: 10_000 });
		// Closed means closed: still closed across two preflight polls (3 s each).
		await page.waitForTimeout(7_000);
		await expect(setupDialog(page)).toHaveCount(0);
		await allTracksRowCount(page, TRACKS);
	});

	test('first-run screens show a new user no endpoints, error codes or env-var names (#2590)', async ({ page }) => {
		test.fail(true, 'issue #2590: the wizard footnote names /api/v1/setup and detection shows raw codes');
		await landAndTimeWizard(page, engine.origin);
		const dialog = setupDialog(page);
		const seen: string[] = [await dialog.innerText()];
		await dialog.getByRole('button', { name: 'Get started' }).click();
		await dialog.getByLabel('A rekordbox collection on this machine').check();
		await expect(dialog.getByRole('heading', { name: 'What is on this machine' })).toBeVisible();
		seen.push(await dialog.innerText());
		await dialog.getByLabel('A folder of audio files (no rekordbox needed)').check();
		seen.push(await dialog.innerText());

		const internals = [/\/api\/v1\//, /\brekordbox_not_found\b/, /\b[A-Z][A-Z0-9]*_[A-Z0-9_]{3,}\b/];
		const leaks = seen.flatMap((text) => internals.filter((pattern) => pattern.test(text)).map(String));
		expect(leaks, 'internals visible on a first-run screen').toEqual([]);
	});
});
