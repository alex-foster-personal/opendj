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
});
