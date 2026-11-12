import { expect, test } from '@playwright/test';

test('keyboard preset switches via ui-prefs without reload', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.keyboard.press('ControlOrMeta+Comma');
	const search = page.locator('input.so-search');
	await expect(search).toBeVisible({ timeout: 15_000 });
	await search.fill('keyboard');
	const select = page.getByLabel('Keyboard layout');
	await expect(select).toBeVisible({ timeout: 15_000 });
	await page.request.put('/api/v1/ui-prefs', { data: { keyboard: { preset: 'serato' } } });
	await expect(select).toHaveValue('serato');
	await expect(page.getByRole('heading', { name: 'Not bound in this layout' })).toBeVisible();
	await page.request.put('/api/v1/ui-prefs', { data: { keyboard: { preset: 'opendj' } } });
});
