/**
 * CMDK-01..03: the Cmd-K command bar on /performance, against the real root
 * fixture library and engine (no route mocks).
 *
 * Acceptance tests:
 *
 * - [if] Ctrl/Cmd-K does not open the bar with the open playlist's rows
 *   listed [then ⛔️] the open test passes.
 * - [if] typing does not narrow the list to the matching track, or Tab does
 *   not switch to whole-library results [then ⛔️] the search test passes.
 * - [if] Right then Enter does not land the highlighted track on deck 2 (read
 *   from the performance IPC, not the DOM) [then ⛔️] the load test passes.
 */
import { expect, test, type Page } from '@playwright/test';

import type { DeckId } from '../../src/lib/rb/deck-id';
import { spendBootLanding } from './support/boot-landing';

const TRACK_ROW = '[data-testid="track-row"]';
const MOD = process.platform === 'darwin' ? 'Meta' : 'Control';

async function openGigWithAllTracks(page: Page): Promise<void> {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 30_000
	});
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator(TRACK_ROW).first()).toBeVisible({ timeout: 30_000 });
}

async function deckStableId(page: Page, deck: DeckId): Promise<string | null> {
	return page.evaluate((id) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query().decks[id]?.stable_id ?? null;
	}, deck);
}

test('Cmd-K opens on the open playlist and Escape closes it', async ({ page }) => {
	test.fixme(true, 'v1 Cmd-K bar test ids, cmdk.v2 is default-on since #10452, see #13521');
	await openGigWithAllTracks(page);
	const tableRows = await page.locator(TRACK_ROW).count();
	await page.keyboard.press(`${MOD}+k`);
	const bar = page.getByTestId('command-bar');
	await expect(bar).toBeVisible();
	await expect(page.getByTestId('command-bar-input')).toBeFocused();
	await expect(page.getByTestId('command-bar-scope')).toHaveAttribute('data-scope', 'playlist');
	await expect(page.getByTestId('command-bar-row')).toHaveCount(Math.min(tableRows, 50));
	await page.keyboard.press('Escape');
	await expect(bar).toBeHidden();
});

test('typing narrows the playlist and Tab searches the whole library', async ({ page }) => {
	test.fixme(true, 'v1 Cmd-K bar test ids, cmdk.v2 is default-on since #10452, see #13521');
	await openGigWithAllTracks(page);
	await page.keyboard.press(`${MOD}+k`);
	const rows = page.getByTestId('command-bar-row');
	const before = await rows.count();
	expect(before).toBeGreaterThan(1);
	const title = (await rows.last().locator('.t').innerText()).trim();
	const word = title.split(/\s+/)[0];
	expect(word).not.toBe('');
	await page.getByTestId('command-bar-input').fill(word);
	await expect(rows.first()).toContainText(word);
	expect(await rows.count()).toBeLessThan(before);

	await page.keyboard.press('Tab');
	await expect(page.getByTestId('command-bar-scope')).toHaveAttribute('data-scope', 'library');
	await expect(page.getByTestId('command-bar-input')).toBeFocused();
	await expect(page.getByTestId('command-bar-row').first()).toContainText(word, {
		timeout: 15_000
	});
});

test('Right then Enter loads the highlighted track onto deck 2', async ({ page }) => {
	test.fixme(true, 'v1 Cmd-K bar test ids, cmdk.v2 is default-on since #10452, see #13521');
	await openGigWithAllTracks(page);
	expect(await deckStableId(page, 2)).toBeNull();
	await page.keyboard.press(`${MOD}+k`);
	const highlighted = page.locator('[data-testid="command-bar-row"][aria-selected="true"]');
	const stableId = await highlighted.getAttribute('data-stable-id');
	expect(stableId).not.toBeNull();
	await expect(page.getByTestId('command-bar-deck-1')).toHaveAttribute('aria-pressed', 'true');
	await page.keyboard.press('ArrowRight');
	await expect(page.getByTestId('command-bar-deck-2')).toHaveAttribute('aria-pressed', 'true');
	await page.keyboard.press('Enter');
	await expect(page.getByTestId('command-bar')).toBeHidden();
	await expect.poll(() => deckStableId(page, 2), { timeout: 45_000 }).toBe(stableId);
	expect(await deckStableId(page, 1)).toBeNull();
});

test.describe('v2 palette on every route (#9467)', () => {
	test.beforeEach(async ({ page }) => {
		await page.route('**/api/v1/flags', async (route) => {
			const response = await route.fetch();
			const body = await response.json();
			const on = new Set(['cmdk.v2', 'cmdk.v2.actions']);
			const flags = body.flags.filter((f: { flag_id: string }) => !on.has(f.flag_id));
			for (const flag_id of on) {
				flags.push({ flag_id, enabled: true, default: false, overridden: true, refusal: null });
			}
			await route.fulfill({ response, json: { ...body, flags } });
		});
	});

	async function openPalette(page: Page, path: string): Promise<void> {
		if (path === '/') await spendBootLanding(page);
		await page.goto(path);
		if (path === '/performance') {
			await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
				timeout: 30_000
			});
		} else if (path === '/') {
			await expect(page.getByTestId('library-workspace')).toBeVisible({ timeout: 30_000 });
		} else {
			await expect(page.getByTestId('practice-page')).toBeVisible({ timeout: 30_000 });
		}
		await expect
			.poll(
				async () => {
					const bar = page.getByTestId('command-bar');
					if (!(await bar.isVisible())) await page.keyboard.press(`${MOD}+k`);
					return bar.isVisible();
				},
				{ timeout: 30_000 }
			)
			.toBe(true);
	}

	test('Cmd-K opens on /performance, the library workspace and /practice/ddj-flx4', async ({
		page
	}) => {
		for (const path of ['/performance', '/', '/practice/ddj-flx4']) {
			await openPalette(page, path);
			await expect(page.getByTestId('command-bar-input')).toBeFocused();
			await page.keyboard.press('Escape');
			await expect(page.getByTestId('command-bar')).toBeHidden();
		}
		await openPalette(page, '/performance');
		await expect(page.getByTestId('command-bar')).toHaveCount(1);
	});

	test('no browser panel shows commands only, not track groups', async ({ page }) => {
		await openGigWithAllTracks(page);
		await page.keyboard.press(`${MOD}+k`);
		const title = (
			await page.getByTestId('command-bar-row').first().locator('.t').innerText()
		).trim();
		expect(title).not.toBe('');
		await page.getByTestId('command-bar-input').fill(title);
		await expect(
			page.locator(
				'[data-testid="command-bar-group"][data-group="playlist"], [data-testid="command-bar-group"][data-group="collection"]'
			)
		).not.toHaveCount(0);
		await page.keyboard.press('Escape');

		await page.goto('/practice/ddj-flx4');
		await expect(page.getByTestId('practice-page')).toBeVisible({ timeout: 30_000 });
		await expect
			.poll(
				async () => {
					const bar = page.getByTestId('command-bar');
					if (!(await bar.isVisible())) await page.keyboard.press(`${MOD}+k`);
					return bar.isVisible();
				},
				{ timeout: 30_000 }
			)
			.toBe(true);
		await expect(page.getByTestId('command-bar')).toHaveAttribute('data-tracks', 'off');
		const input = page.getByTestId('command-bar-input');
		await input.fill(title);
		await expect(input).toBeEditable();
		await expect(page.getByTestId('command-bar-row')).toHaveCount(0);
		await expect(
			page.locator('[data-testid="command-bar-group"][data-group="playlist"]')
		).toHaveCount(0);
		await expect(
			page.locator('[data-testid="command-bar-group"][data-group="collection"]')
		).toHaveCount(0);

		await input.fill('theme');
		await expect(page.getByTestId('command-bar-action').first()).toBeVisible({ timeout: 15_000 });
	});
});
