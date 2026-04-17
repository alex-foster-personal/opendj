import { test, expect } from '@playwright/test';

for (const path of ['/', '/pairings', '/queues']) {
	test(`smoke: ${path} renders without console errors`, async ({ page }) => {
		const errors: string[] = [];
		page.on('console', (msg) => {
			if (msg.type() === 'error') errors.push(msg.text());
		});
		await page.goto(path);
		await expect(page.locator('body')).toBeVisible();
		expect(errors.filter((e) => !e.includes('favicon'))).toEqual([]);
	});
}
