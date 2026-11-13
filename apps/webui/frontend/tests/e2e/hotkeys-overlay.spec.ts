/**
 * Issue #12278: the hotkeys overlay filter's Escape sequence.
 *
 * [if] ? opens the overlay [then] its filter receives focus [else stop].
 * [if] filter text is present and Escape is pressed [then] the filter clears while the
 * overlay remains open [else stop].
 * [if] the empty filter receives Escape [then] the overlay closes [else stop].
 */
import { expect, test } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

test('Hotkeys filter: ? focuses it, Esc clears it, Esc again closes the overlay', async ({
	page
}) => {
	await page.goto('/performance');
	const rows = page.locator('[data-testid="track-row"]');
	await expect(rows.first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);

	const dialog = page.getByRole('dialog', { name: 'Hotkeys' });
	await expect(async () => {
		if (!(await dialog.isVisible())) await page.keyboard.press('?');
		await expect(dialog).toBeVisible();
	}).toPass({ timeout: 45_000 });

	const filter = dialog.getByLabel('Filter hotkeys');
	await expect(filter).toBeFocused();

	await page.keyboard.type('deck');
	await expect(filter).toHaveValue('deck');
	await expect(dialog.locator('mark.hk-mark').first()).toBeVisible();

	await page.keyboard.press('Escape');
	await expect(filter).toHaveValue('');
	await expect(dialog).toBeVisible();
	await expect(filter).toBeFocused();
	await expect(dialog.locator('mark.hk-mark')).toHaveCount(0);

	await page.keyboard.press('Escape');
	await expect(dialog).toBeHidden();
});
