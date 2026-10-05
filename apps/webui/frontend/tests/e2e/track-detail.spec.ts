import { test, expect } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';

test.describe('CAT-05a track detail page', () => {
	// `page.goto('/')` below is a cold open and would race PERFMODE-11's one-shot
	// redirect into Gig; support/boot-landing.ts has the full account.
	test.beforeEach(async ({ page }) => {
		await spendBootLanding(page);
	});

	test('renders provenance + rating control', async ({ page }) => {
		await page.goto('/');
		await page.locator('table.library tbody tr').first().click();
		await expect(page.locator('h2')).toBeVisible();
		await expect(page.getByRole('radio', { name: /^Set rating [1-5]$/ })).toHaveCount(5);
	});
});
