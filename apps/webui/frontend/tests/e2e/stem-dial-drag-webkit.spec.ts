// requirement: STEMDIAL-DRAG-9210
/**
 * WebKit production-artifact acceptance for the DRUMS dial beside FILTER.
 *
 * WKWebView can stop retargeting captured pointer moves once the cursor leaves
 * this tight slot. These tests run only in playwright.webkit-deckload.config.ts
 * against the engine-served build, attach a real demucs4 bundle to the existing
 * fixture, and load without playing so the #637/#680 AudioContext path stays out.
 */
import { execFileSync } from 'node:child_process';
import { rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

import { expect, test, type Locator, type Page } from '@playwright/test';

import { FIXTURE_DATA_DIR } from './playwright.webkit-deckload.config';
import { waitForPerformanceIpc } from './support/performance-ready';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../../', import.meta.url));
// The virtualenv layout depends on the host: Windows puts the interpreter in
// Scripts\python.exe, POSIX in bin/python (review thread PRRT_kwDOSEvNd860y_yo).
const VENV_PYTHON =
	process.platform === 'win32'
		? join(REPOSITORY_ROOT, '.venv', 'Scripts', 'python.exe')
		: join(REPOSITORY_ROOT, '.venv', 'bin', 'python');
const TRACK_ROW = '[data-testid="track-row"]';
const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const FIXTURE_SECONDS = 60;
let attachedStableId: string | null = null;

function valueOf(dial: Locator): Promise<number> {
	return dial.getAttribute('aria-valuenow').then((raw) => {
		const value = raw === null ? Number.NaN : Number.parseFloat(raw);
		if (!Number.isFinite(value)) throw new Error(`dial aria-valuenow is not numeric: ${raw}`);
		return value;
	});
}

function attachStemBundle(stableId: string): void {
	attachedStableId = stableId;
	execFileSync(
		VENV_PYTHON,
		[
			'-m',
			'apps.webui.frontend.tests.e2e.support.stem_decode_fixture',
			'--data-dir',
			FIXTURE_DATA_DIR,
			'--attach-stable-id',
			stableId,
			'--seconds',
			String(FIXTURE_SECONDS)
		],
		{ cwd: REPOSITORY_ROOT, encoding: 'utf-8', timeout: 180_000 }
	);
}

function removeAttachedStemBundle(): void {
	if (attachedStableId === null) return;
	rmSync(join(FIXTURE_DATA_DIR, 'state', 'stems', attachedStableId), {
		recursive: true,
		force: true
	});
	attachedStableId = null;
}

async function loadTrackWithStems(page: Page): Promise<{ drums: Locator; filter: Locator }> {
	const row = page.locator(TRACK_ROW).first();
	const stableId = await row.getAttribute('data-stable-id');
	if (stableId === null) throw new Error('fixture row has no stable id');
	attachStemBundle(stableId);

	const titleCell = row.locator('td.c-title');
	await titleCell.click();
	await expect(row, 'the row must select before its load-only control is live').toHaveClass(
		/rb-row-selected/
	);
	await titleCell.hover();
	await row.locator('.deck-btns').getByRole('button', { name: '1', exact: true }).click();

	await page.waitForFunction(
		(id) => {
			const deck = window.musicDjToolsPerformance?.query().decks[1];
			return (
				deck?.stable_id === id &&
				deck.transport_pending === false &&
				deck.stems.status === 'ready' &&
				deck.stems.available_controls.includes('drums')
			);
		},
		stableId,
		{ timeout: 60_000 }
	);
	await page.evaluate(async () => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch({ type: 'mixer_stems', enabled: true });
	});

	const drums = page.getByTestId('knob-1:stem-drums');
	const filter = page.getByTestId('knob-1:filter');
	await expect(drums).toBeVisible();
	await expect(filter).toBeVisible();
	await expect(filter).toHaveAttribute('aria-disabled', 'false');
	expect(await drums.evaluate((el) => el.closest('[data-rust-inert]') === null)).toBe(true);
	return { drums, filter };
}

test.beforeEach(async ({ page }) => {
	await page.addInitScript((prefsKey) => {
		window.localStorage.setItem(prefsKey, JSON.stringify({ hide_broken_links: false }));
	}, PREFS_STORAGE_KEY);
	await page.goto('/performance');
	await waitForPerformanceIpc(page);
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator(TRACK_ROW).first()).toBeVisible({ timeout: 30_000 });
});

test.afterEach(() => {
	removeAttachedStemBundle();
});

test('a real WebKit drag that leaves the DRUMS dial changes DRUMS, not FILTER', async ({
	page
}) => {
	const { drums, filter } = await loadTrackWithStems(page);
	const drumsBefore = await valueOf(drums);
	const filterBefore = await valueOf(filter);
	const box = await drums.boundingBox();
	if (box === null) throw new Error('DRUMS dial has no bounding box');
	const x = box.x + box.width / 2;
	const y = box.y + box.height / 2;

	await page.mouse.move(x, y);
	await page.mouse.down();
	await page.mouse.move(x, y - 80, { steps: 8 });
	await page.mouse.up();

	await expect.poll(() => valueOf(drums)).toBeGreaterThan(drumsBefore + 0.1);
	expect(await valueOf(filter)).toBe(filterBefore);
	await expect(page.getByTestId('color-fx-menu-1')).toHaveCount(0);
});

test('scroll still changes the DRUMS dial on FILTER', async ({ page }) => {
	const { drums, filter } = await loadTrackWithStems(page);
	const drumsBefore = await valueOf(drums);
	const filterBefore = await valueOf(filter);

	await drums.hover();
	await page.mouse.wheel(0, 100);

	await expect
		.poll(async () => Math.abs((await valueOf(drums)) - drumsBefore))
		.toBeGreaterThan(0.01);
	expect(await valueOf(filter)).toBe(filterBefore);
	await expect(page.getByTestId('color-fx-menu-1')).toHaveCount(0);
});
