import { test, expect } from '@playwright/test';

test.describe('CAT-05a track detail page', () => {
	test('renders provenance + rating control', async ({ page }) => {
		await page.goto('/');
		await page.locator('table.library tbody tr').first().click();
		await expect(page.locator('h2')).toBeVisible();
		await expect(page.locator('.star')).toHaveCount(5);
	});
});
