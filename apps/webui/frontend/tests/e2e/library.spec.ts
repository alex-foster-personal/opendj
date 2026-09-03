import { test, expect } from '@playwright/test';

test.describe('CAT-05a library page', () => {
	test('lists tracks and navigates to detail', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('table.library')).toBeVisible();
		await page.locator('table.library tbody tr').first().click();
		await expect(page).toHaveURL(/\/track\//);
	});

	test('filter narrows the list', async ({ page }) => {
		await page.goto('/');
		await page.fill('input[placeholder="Search title/artist"]', 'midnight');
		await page.waitForTimeout(300);
		const rows = await page.locator('table.library tbody tr').count();
		expect(rows).toBeLessThan(10);
	});

	// PREF-01: right-click a track's BPM cell to set a preferred tempo plus a
	// min/max playable range, persist it, and read it back through the same
	// popover on reopen.
	test('right-click BPM opens tempo-pref editor and round-trips a range', async ({ page }) => {
		await page.goto('/');
		const bpmCell = page.locator('table.library tbody tr').first().locator('td.c-bpm');
		await expect(bpmCell).toBeVisible();

		await bpmCell.click({ button: 'right' });
		const popover = page.locator('.tempo-pref-popover');
		await expect(popover).toBeVisible();

		const regularInput = popover.locator('.tp-regular input');
		const minInput = popover.locator('.tp-minmax').nth(0).locator('input');
		const maxInput = popover.locator('.tp-minmax').nth(1).locator('input');

		await regularInput.fill('140');
		await minInput.fill('138');
		await maxInput.fill('142');
		await popover.getByRole('button', { name: 'Save' }).click();
		await expect(popover).not.toBeVisible();

		await bpmCell.click({ button: 'right' });
		await expect(popover).toBeVisible();
		await expect(regularInput).toHaveValue('140');
		await expect(minInput).toHaveValue('138');
		await expect(maxInput).toHaveValue('142');
		await page.keyboard.press('Escape');
		await expect(popover).not.toBeVisible();
	});

	test('tempo-pref editor rejects min >= max', async ({ page }) => {
		await page.goto('/');
		const bpmCell = page.locator('table.library tbody tr').first().locator('td.c-bpm');
		await bpmCell.click({ button: 'right' });
		const popover = page.locator('.tempo-pref-popover');
		await expect(popover).toBeVisible();

		await popover.locator('.tp-minmax').nth(0).locator('input').fill('150');
		await popover.locator('.tp-minmax').nth(1).locator('input').fill('140');
		await expect(popover.getByText('min must be less than max')).toBeVisible();
		await expect(popover.getByRole('button', { name: 'Save' })).toBeDisabled();
		await page.keyboard.press('Escape');
	});
});
