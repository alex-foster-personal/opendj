/**
 * SKINLAYOUT-10203: deck column compact tier hides priority-3 parts under 360px.
 *
 * [if] at 1280x800 deck artwork, pad labels and BeatJump are visible [then STOP]
 * [if] the deck column is forced to 340px wide [then] artwork, pad labels and
 *      BeatJump hide while hot-cue letters stay visible
 * [if] the deck column is forced to 290px wide [then] loop column and grid-adjust hide
 *
 * RUN: pnpm exec playwright test performance-deck-compact-tier
 */
import { expect, test } from '@playwright/test';

const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const TRACK_ROW = '[data-testid="track-row"]';

test('deck compact and narrow container tiers', async ({ page }) => {
	test.setTimeout(120_000);
	await page.addInitScript((prefsKey) => {
		window.localStorage.setItem(prefsKey, JSON.stringify({ hide_broken_links: false }));
	}, PREFS_STORAGE_KEY);

	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/performance?playlist=all');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const firstRow = page.locator(TRACK_ROW).first();
	await expect(firstRow).toBeVisible({ timeout: 30_000 });
	const stableId = await firstRow.getAttribute('data-stable-id');
	expect(stableId, 'fixture library must expose a track row').toBeTruthy();
	await page.evaluate(async (id) => {
		await window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 1, stable_id: id });
	}, stableId);
	await page.waitForFunction(
		() => window.musicDjToolsPerformance!.query().decks[1].stable_id !== null,
		undefined,
		{ timeout: 30_000 }
	);

	const deck1 = page.locator('section.rb-deck[data-deck="1"]');
	await expect(deck1).toBeVisible({ timeout: 30_000 });

	const padA = page.getByTestId('hot-cue-1-A');
	await padA.click();
	const renameInput = page.getByTestId('hot-cue-name-1-A');
	await expect(renameInput).toBeVisible({ timeout: 15_000 });
	await renameInput.press('Enter');
	await expect(padA.locator('.cue-label')).toBeVisible({ timeout: 15_000 });

	const art = deck1.locator('.art-slot');
	const cueLabel = deck1.locator('.cue-label').first();
	const beatJump = deck1.locator('.beat-jump');
	const loopCol = deck1.locator('.loop-col');
	const gridAdjust = page.getByTestId('grid-adjust-deck-1');

	await expect(art).toBeVisible();
	await expect(cueLabel).toBeVisible();
	await expect(beatJump).toBeVisible();
	await expect(loopCol).toBeVisible();
	await expect(gridAdjust).toBeVisible();

	const firstCol = page.locator('.deck-area > .deck-col').first();
	const defaultWidth = await firstCol.evaluate((el) => el.getBoundingClientRect().width);
	expect(defaultWidth).toBeGreaterThan(360);

	await page.addStyleTag({
		content:
			'.deck-area > .deck-col:first-child { width: 340px !important; max-width: 340px !important; justify-self: start; }'
	});
	await page.evaluate(
		() =>
			new Promise<void>((resolve) =>
				requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
			)
	);

	const compactWidth = await firstCol.evaluate((el) => el.getBoundingClientRect().width);
	expect(compactWidth).toBeLessThanOrEqual(340);

	await expect(art).toBeHidden();
	await expect(cueLabel).toBeHidden();
	await expect(beatJump).toBeHidden();
	await expect(padA).toBeVisible();
	await expect(padA.locator('.letter')).toHaveText('A');
	await expect(loopCol).toBeVisible();
	await expect(gridAdjust).toBeVisible();

	await page.addStyleTag({
		content:
			'.deck-area > .deck-col:first-child { width: 290px !important; max-width: 290px !important; justify-self: start; }'
	});
	await page.evaluate(
		() =>
			new Promise<void>((resolve) =>
				requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
			)
	);

	const narrowWidth = await firstCol.evaluate((el) => el.getBoundingClientRect().width);
	expect(narrowWidth).toBeLessThanOrEqual(290);
	await expect(loopCol).toBeHidden();
	await expect(gridAdjust).toBeHidden();
});
