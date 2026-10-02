/**
 * SETUP-22: the folders a real new user points the wizard at, adversarially.
 *
 * Every case starts on a brand-new packaged engine (support/onboarding-engine)
 * with the wizard opened by the first-run gate, and every folder is real bytes
 * on disk. Nothing is mocked: a denied folder is a chmod 000 directory, a
 * missing one is a path nothing exists at, and a corrupt file is bytes that are
 * not audio wearing a .wav name.
 *
 * Single-line acceptance checks:
 * - if a denied folder is reported as an empty one, or offers Import -> broken.
 * - if a missing folder offers Import, or leaves the user no way forward -> broken.
 * - if a folder path with NFD unicode and repeated spaces does not import every
 *   file, or its titles reach All Tracks mangled -> broken.
 * - if corrupt and zero-byte files become tracks, or take the good ones down
 *   with them -> broken.
 */
import { expect, test } from '@playwright/test';
import { chmodSync, mkdirSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

import {
	type OnboardingEngine,
	startOnboardingEngine,
	writeMusicFolder,
	writeUnplayableFiles
} from './support/onboarding-engine';
import {
	allTracksRowCount,
	checkFolder,
	chooseFolderBranch,
	continueToDone,
	expectFolderEscapesEnabled,
	landAndTimeWizard,
	minimiseWizard,
	readSetupStatus,
	setupDialog,
	startFolderImport,
	waitForTerminalJob
} from './support/onboarding-wizard';

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

/** Count every import POST the page makes; a refused folder must make none. */
function countImportPosts(page: import('@playwright/test').Page): () => number {
	let posts = 0;
	page.on('request', (request) => {
		if (request.method() === 'POST' && request.url().includes('/api/v1/setup/import')) posts += 1;
	});
	return () => posts;
}

test.describe('onboarding gauntlet: adversarial folders', () => {
	test('a folder the OS refuses to list says so, never reads as empty, and offers no import', async ({ page }) => {
		if (process.getuid?.() === 0) {
			throw new Error('running as root: a chmod 000 folder is still listable, so the denied case cannot be arranged');
		}
		const denied = join(engine.musicRoot, 'Locked Crate');
		writeMusicFolder(denied, 3);
		chmodSync(denied, 0o000);
		// Control: the arrangement really is a refusal for this user, or the case proves nothing.
		expect(() => readdirSync(denied)).toThrow(/EACCES|EPERM/);
		const imports = countImportPosts(page);

		await landAndTimeWizard(page, engine.origin);
		await chooseFolderBranch(page);
		const scan = await checkFolder(page, denied);
		expect(scan).toMatchObject({ exists: true, readable: false, denied: true, audio_files: 0 });

		const dialog = setupDialog(page);
		await expect(dialog.getByRole('alert').filter({ hasText: 'refused the listing' })).toBeVisible();
		// A count taken behind a permission wall is a count of nothing: never quoted.
		await expect(dialog.getByText(/\b0 audio files\b/)).toHaveCount(0);
		await expect(dialog.getByRole('button', { name: 'Import this folder' })).toBeDisabled();
		await expect(dialog.getByText('macOS is blocking that folder', { exact: false })).toBeVisible();
		await expectFolderEscapesEnabled(page);
		expect(imports()).toBe(0);
		expect((await readSetupStatus(engine.origin)).library_empty).toBe(true);
	});

	test('a folder that does not exist is named as missing and the user keeps every way forward', async ({ page }) => {
		const missing = join(engine.musicRoot, 'Typo Crate That Was Never Made');
		const imports = countImportPosts(page);

		await landAndTimeWizard(page, engine.origin);
		await chooseFolderBranch(page);
		const scan = await checkFolder(page, missing);
		expect(scan).toMatchObject({ exists: false, readable: false, denied: false, audio_files: 0 });

		const dialog = setupDialog(page);
		await expect(dialog.getByText(`Nothing at ${missing}.`)).toBeVisible();
		await expect(dialog.getByRole('button', { name: 'Import this folder' })).toBeDisabled();
		await expectFolderEscapesEnabled(page);
		expect(imports()).toBe(0);

		// The way forward is real: fix the path and the same step imports it.
		const real = join(engine.musicRoot, 'The Real Crate');
		writeMusicFolder(real, 2);
		const rescan = await checkFolder(page, real);
		expect(rescan).toMatchObject({ readable: true, audio_files: 2 });
		await expect(dialog.getByRole('button', { name: 'Import this folder' })).toBeEnabled();
	});

	test('a folder path with NFD unicode and repeated spaces imports every track, titles intact', async ({ page }) => {
		// NFD on purpose: what the macOS picker and Finder hand over, and what a
		// naive NFC normalization on the way to the engine would break on Linux.
		const folderName = 'Café  Crème Mix'.normalize('NFD');
		const prefix = 'Señor Níño'.normalize('NFD');
		const folder = join(engine.musicRoot, folderName);
		writeMusicFolder(folder, 3, prefix);

		await landAndTimeWizard(page, engine.origin);
		await chooseFolderBranch(page);
		const scan = await checkFolder(page, folder);
		expect(scan).toMatchObject({ readable: true, denied: false, audio_files: 3 });

		const jobId = await startFolderImport(page);
		const job = await waitForTerminalJob(engine.origin, jobId);
		expect(job.status, JSON.stringify(job)).toBe('succeeded');
		await continueToDone(page);
		await minimiseWizard(page);
		await allTracksRowCount(page, 3);

		const rowText = await page.locator('[data-testid="track-row"]').allInnerTexts();
		const wanted = prefix.normalize('NFC');
		const intact = rowText.filter((text) => text.normalize('NFC').includes(wanted));
		expect(intact.length, `rows: ${JSON.stringify(rowText)}`).toBe(3);
	});

	test('corrupt and zero-byte files are skipped and the playable ones still import', async ({ page }) => {
		const folder = join(engine.musicRoot, 'Mixed Bag');
		mkdirSync(folder, { recursive: true });
		const good = writeMusicFolder(folder, 2, 'Playable');
		const bad = writeUnplayableFiles(folder);

		await landAndTimeWizard(page, engine.origin);
		await chooseFolderBranch(page);
		await checkFolder(page, folder);

		const jobId = await startFolderImport(page);
		const job = await waitForTerminalJob(engine.origin, jobId);
		expect(job.status, JSON.stringify(job)).toBe('succeeded');

		const status = await readSetupStatus(engine.origin);
		expect(status.last_import).toMatchObject({
			kind: 'folder',
			tracks_written: good.length,
			files_rejected_unplayable: bad.length
		});
		await continueToDone(page);
		await minimiseWizard(page);
		await allTracksRowCount(page, good.length);
	});
});
