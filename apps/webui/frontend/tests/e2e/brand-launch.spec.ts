// requirement: OPS-10
// requirement: PERF-UI-03
import { expect, test } from '@playwright/test';

test('first open plays the identity launch once without blocking the app', async ({ page }) => {
	await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
	await page.goto('/');

	const launch = page.getByLabel('Open DJ launch animation');
	await expect(launch).toBeVisible();
	await expect(page.locator('body')).toBeVisible();
	await expect(launch).toBeHidden({ timeout: 5_000 });
	await expect.poll(() => page.evaluate(() => localStorage.getItem('odj.brand-launch.v1'))).toBe('complete');

	await page.reload();
	await expect(launch).toBeHidden();
});

test('library rows paint behind the launch overlay on /performance', async ({ page }) => {
	test.skip(process.env.PERFORMANCE_E2E_FIXTURE === '1', 'needs reference library rows');
	await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
	await page.goto('/performance');
	const launch = page.getByLabel('Open DJ launch animation');
	await expect(launch).toBeVisible();
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({
		timeout: 15_000
	});
	await expect(launch).toBeVisible();
});
