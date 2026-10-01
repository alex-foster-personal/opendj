/**
 * SETUP-23: a new user's first run when something goes wrong under them.
 *
 * Skip-then-reload, a network that drops, and an engine that dies mid-import.
 * Each case boots a brand-new packaged engine (support/onboarding-engine).
 * The ONLY injected faults are network faults (page.route abort and the
 * browser's offline switch) and a real SIGKILL of the engine's process group;
 * every answer the page receives is one the real engine served.
 *
 * Single-line acceptance checks:
 * - if "Skip for now" does not close the wizard into the incomplete note -> broken
 *   (#3422, test.fail).
 * - if a reload after skipping re-traps the user in the wizard, or setup can
 *   no longer be reached at all -> broken.
 * - if a failed import request strands the wizard, or a retry cannot import -> broken.
 * - if the browser going offline mid-import leaves the wizard short of
 *   `succeeded` once it is back online, without a reload -> broken.
 * - if an engine killed mid-import leaves a job that is live forever, refuses
 *   the next import, or doubles rows on retry -> broken.
 */
import { expect, test } from '@playwright/test';
import { join } from 'node:path';

import { type OnboardingEngine, startOnboardingEngine, writeMusicFolder } from './support/onboarding-engine';
import {
	allTracksRowCount,
	checkFolder,
	chooseFolderBranch,
	continueToDone,
	IMPORT_BUDGET_MS,
	landAndTimeWizard,
	minimiseWizard,
	readJob,
	readSetupStatus,
	setupDialog,
	startFolderImport,
	waitForTerminalJob
} from './support/onboarding-wizard';

/** Two boot-gate preflight polls (PreflightScreen POLL_MS = 3 s) plus slack. */
const STAYS_CLOSED_MS = 7_000;
/** Enough tiny files that a kill lands while the job is still running. */
const KILL_FIXTURE_FILES = 400;

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

test.describe('onboarding gauntlet: recovery', () => {
	test('"Skip for now" closes the wizard into the incomplete note and it stays closed (#3422)', async ({ page }) => {
		await landAndTimeWizard(page, engine.origin);
		const dialog = setupDialog(page);
		const dismissed = page.waitForResponse((response) => response.url().includes('/api/v1/setup/dismiss'));
		await dialog.getByRole('button', { name: 'Skip for now' }).click();
		expect((await dismissed).ok(), 'the engine refused the dismissal').toBe(true);
		expect(await readSetupStatus(engine.origin)).toMatchObject({ dismissed: true, should_show_wizard: false });
		// The engine recorded the skip; only the overlay bouncing back is the known defect.
		test.fail(true, 'issue #3422: the empty-library reopen predicate raises the wizard again the moment it closes');
		await expect(dialog).toHaveCount(0);
		await expect(page.getByText('Setup incomplete -- library is', { exact: false })).toBeVisible();
		await page.waitForTimeout(STAYS_CLOSED_MS);
		await expect(dialog).toHaveCount(0);
	});

	test('after "Skip for now", a reload does not re-trap the user and /setup still opens the wizard', async ({ page }) => {
		await landAndTimeWizard(page, engine.origin);
		const dismissed = page.waitForResponse((response) => response.url().includes('/api/v1/setup/dismiss'));
		await setupDialog(page).getByRole('button', { name: 'Skip for now' }).click();
		expect((await dismissed).ok()).toBe(true);
		expect(await readSetupStatus(engine.origin)).toMatchObject({ dismissed: true, should_show_wizard: false });

		await page.reload();
		await page.waitForResponse((response) => response.url().includes('/api/v1/setup/status'));
		await page.waitForTimeout(STAYS_CLOSED_MS);
		await expect(setupDialog(page)).toHaveCount(0);

		// The door back in still works: /setup opens the same wizard.
		await page.goto(`${engine.origin}/setup`);
		await expect(setupDialog(page)).toBeVisible();
	});

	test('an import request lost to the network leaves the wizard on its step, and a retry imports', async ({ page }) => {
		const music = join(engine.musicRoot, 'My Music');
		writeMusicFolder(music, 3);
		await landAndTimeWizard(page, engine.origin);
		await chooseFolderBranch(page);
		await checkFolder(page, music);

		const dialog = setupDialog(page);
		await page.route('**/api/v1/setup/import/folder', (route) => route.abort('internetdisconnected'));
		await dialog.getByRole('button', { name: 'Import this folder' }).click();
		await expect(dialog.getByRole('alert').first()).toBeVisible();
		await expect(dialog.getByRole('heading', { name: 'Point at a folder' })).toBeVisible();
		expect((await readSetupStatus(engine.origin)).library_empty).toBe(true);

		await page.unroute('**/api/v1/setup/import/folder');
		await expect(dialog.getByRole('button', { name: 'Import this folder' })).toBeEnabled();
		const jobId = await startFolderImport(page);
		expect((await waitForTerminalJob(engine.origin, jobId)).status).toBe('succeeded');
		await continueToDone(page);
		await minimiseWizard(page);
		await allTracksRowCount(page, 3);
	});

	test('the browser going offline mid-import still ends on succeeded once it is back', async ({ page, context }) => {
		const music = join(engine.musicRoot, 'Big Crate');
		writeMusicFolder(music, KILL_FIXTURE_FILES, 'Offline Track', 0.05);
		await landAndTimeWizard(page, engine.origin);
		await chooseFolderBranch(page);
		await checkFolder(page, music);

		const jobId = await startFolderImport(page);
		await context.setOffline(true);
		// Control: the drop has to land while the job is live, or this proves nothing.
		const atDrop = await readJob(engine.origin, jobId);
		expect(['queued', 'running'], `job was already ${atDrop.status} when the network dropped`).toContain(atDrop.status);
		const finished = await waitForTerminalJob(engine.origin, jobId);
		expect(finished.status).toBe('succeeded');
		await context.setOffline(false);

		// No reload: the wizard has to find out on its own.
		await expect(setupDialog(page).getByText('succeeded', { exact: false }).first()).toBeVisible({
			timeout: 30_000
		});
	});

	test('an engine killed mid-import relaunches into a recoverable state with no duplicate rows', async ({ page }) => {
		const music = join(engine.musicRoot, 'Big Crate');
		writeMusicFolder(music, KILL_FIXTURE_FILES, 'Crash Track', 0.05);
		await landAndTimeWizard(page, engine.origin);
		await chooseFolderBranch(page);
		await checkFolder(page, music);

		const jobId = await startFolderImport(page);
		await expect.poll(async () => (await readJob(engine.origin, jobId)).status, { timeout: 60_000 }).toBe('running');
		await engine.kill();
		await engine.restart();

		// The interrupted job must not stay live: a live row would 409 every retry.
		const interrupted = await waitForTerminalJob(engine.origin, jobId, 30_000);
		expect(interrupted.status, JSON.stringify(interrupted)).not.toBe('succeeded');

		// The user relaunches the app and goes back to setup.
		await page.goto(`${engine.origin}/setup`);
		await expect(setupDialog(page)).toBeVisible();
		await chooseFolderBranch(page);
		await checkFolder(page, music);
		const retryId = await startFolderImport(page);
		const retried = await waitForTerminalJob(engine.origin, retryId, IMPORT_BUDGET_MS);
		expect(retried.status, JSON.stringify(retried)).toBe('succeeded');

		const status = await readSetupStatus(engine.origin);
		expect(status.tracks, 'a retried import must not duplicate rows').toBe(KILL_FIXTURE_FILES);
	});
});
