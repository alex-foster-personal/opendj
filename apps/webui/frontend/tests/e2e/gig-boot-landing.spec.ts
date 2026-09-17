// requirement: PERFMODE-11
// [if] cold open at `/` with a recent Gig stamp [then] first settled route is `/performance`
// [if] cold open with no stamp [then] v1 still lands on `/performance`

import { expect, test } from '@playwright/test';

import { BOOT_LANDING_SESSION_KEY } from '../../src/lib/rb/boot-landing';

async function clearBootLandingSession(page: import('@playwright/test').Page): Promise<void> {
	await page.addInitScript((key) => {
		sessionStorage.removeItem(key);
	}, BOOT_LANDING_SESSION_KEY);
}

test('recent Gig stamp cold-opens into performance', async ({ page, request }) => {
	const stamp = new Date().toISOString();
	const put = await request.put('/api/v1/ui-prefs', {
		data: { app_mode: { last_gig_at: stamp } }
	});
	expect(put.ok()).toBeTruthy();

	await clearBootLandingSession(page);
	await page.goto('/?muted=1', { waitUntil: 'domcontentloaded' });
	await expect(page).toHaveURL(/\/performance/, { timeout: 30_000 });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect(page.locator('aside.sidebar')).toHaveCount(0);
});

test('missing Gig stamp still cold-opens into performance in v1', async ({ page }) => {
	await clearBootLandingSession(page);
	await page.goto('/?muted=1', { waitUntil: 'domcontentloaded' });
	await expect(page).toHaveURL(/\/performance/, { timeout: 30_000 });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect(page.locator('aside.sidebar')).toHaveCount(0);
});
