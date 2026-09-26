/**
 * DECKUX-21 / LIBUX-21: Chromium drag-to-deck load and Space must not scroll
 * the library. Uses the performance fixture library (playwright.performance.config.ts).
 */
import { expect, test, type Page } from '@playwright/test';

const TRACK_ROW = '[data-testid="track-row"]';
const TRACK_STABLE_MIME = 'application/x-mdt-stable-id';

async function dragRowToDeck(
	page: Page,
	rowIndex: number,
	deckId: number
): Promise<{ dragOverAccepted: boolean; payloadOnDrop: string }> {
	return page.evaluate(
		({ rowSelector, index, deck, mime }) => {
			const row = document.querySelectorAll(rowSelector)[index];
			if (!(row instanceof HTMLElement)) throw new Error(`no track row at index ${index}`);
			const target = document.querySelector(`section.rb-deck[data-deck="${deck}"]`);
			if (!(target instanceof HTMLElement)) throw new Error(`no deck ${deck}`);

			const start = new DataTransfer();
			row.dispatchEvent(
				new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: start })
			);

			const carried = start;
			const over = new DragEvent('dragover', {
				bubbles: true,
				cancelable: true,
				dataTransfer: carried
			});
			target.dispatchEvent(over);
			const report = {
				dragOverAccepted: over.defaultPrevented,
				payloadOnDrop: carried.getData(mime)
			};

			target.dispatchEvent(
				new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: carried })
			);
			row.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: carried }));
			return report;
		},
		{ rowSelector: TRACK_ROW, index: rowIndex, deck: deckId, mime: TRACK_STABLE_MIME }
	);
}

test('performance: drag library row onto deck loads track and Space toggles play without scrolling library', async ({
	page
}) => {
	test.setTimeout(120_000);
	const pageErrors: string[] = [];
	page.on('pageerror', (err) => pageErrors.push(err.message));
	page.on('console', (msg) => {
		if (msg.type() === 'error') pageErrors.push(msg.text());
	});
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});

	const row = page.locator(TRACK_ROW).first();
	await expect(row).toBeVisible({ timeout: 60_000 });
	const stableId = await row.getAttribute('data-stable-id');
	if (stableId === null) throw new Error('first row missing data-stable-id');
	await row.click();

	const gesture = await dragRowToDeck(page, 0, 1);
	expect(gesture.dragOverAccepted, 'deck 1 must accept dragover').toBe(true);
	expect(gesture.payloadOnDrop).toBe(stableId);

	await page.waitForFunction(
		(id) => window.musicDjToolsPerformance?.query().decks[1].stable_id === id,
		stableId,
		{ timeout: 90_000 }
	);

	const tableWrap = page.locator('.table-wrap');
	const scrollBefore = await tableWrap.evaluate((el) => el.scrollTop);
	await page.keyboard.press('Space');
	const scrollAfter = await tableWrap.evaluate((el) => el.scrollTop);
	expect(scrollAfter).toEqual(scrollBefore);

	await page.waitForFunction(
		() => window.musicDjToolsPerformance?.query().decks[1].playing === true,
		undefined,
		{ timeout: 30_000 }
	);
	expect(pageErrors, 'drag and Space must not surface page or console errors').toEqual([]);
});

test('performance: Space on library with no loaded deck does not scroll the table', async ({ page }) => {
	await page.goto('/performance');
	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible({ timeout: 60_000 });
	await tableWrap.click();
	const scrollBefore = await tableWrap.evaluate((el) => el.scrollTop);
	await page.keyboard.press('Space');
	const scrollAfter = await tableWrap.evaluate((el) => el.scrollTop);
	expect(scrollAfter).toEqual(scrollBefore);
});
