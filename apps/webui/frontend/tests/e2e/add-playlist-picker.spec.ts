/**
 * Add-to-playlist picker keyboard cancel and focus return (issue #13395).
 *
 * - [if] the add-to-playlist picker opens from a selected track [then]
 * - [if] Escape closes the picker [then]
 * - [if] the picker closes without a selection [then]
 * - [if] focus returns to the invoking control [then]
 */
import { expect, test, type ElementHandle, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

async function isActiveElement(
	page: Page,
	handle: ElementHandle<SVGElement | HTMLElement> | null
): Promise<boolean> {
	if (!handle) return false;
	return page.evaluate((el) => document.activeElement === el, handle);
}

// REQ: LIBM-95
test('Escape dismisses add-to-playlist picker and returns focus to the track row', async ({
	page
}) => {
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);

	const row = page.locator('[data-testid="track-row"]').first();
	await row.focus();
	const rowHandle = await row.elementHandle();
	expect(await isActiveElement(page, rowHandle)).toBe(true);

	await row.click({ button: 'right' });
	const addItem = page.getByRole('menuitem', { name: 'Add to playlist...' });
	await expect(addItem).toBeEnabled();
	await addItem.click();

	await expect(page.locator('[data-testid="add-to-playlist-picker"]')).toBeVisible();

	await page.keyboard.press('Escape');
	await expect(page.locator('[data-testid="add-to-playlist-picker"]')).toHaveCount(0);
	expect(await isActiveElement(page, rowHandle)).toBe(true);
});
