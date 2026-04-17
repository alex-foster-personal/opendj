import { test, expect } from '@playwright/test';

test.describe('CAT-05a pairings page', () => {
	test('shows empty state or create form', async ({ page }) => {
		await page.goto('/pairings');
		await expect(page.getByRole('heading', { name: 'Pairings' })).toBeVisible();
	});
});
