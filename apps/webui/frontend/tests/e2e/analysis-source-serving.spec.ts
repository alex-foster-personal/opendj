/**
 * PARITY-02 serving registry: unserved lanes disable OWN in the top-bar menu.
 */
import { expect, test } from '@playwright/test';

test('vocal OWN is disabled until the lane is served', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.setViewportSize({ width: 1864, height: 947 });
	await page.locator('.src-toggle').hover();
	await expect(page.locator('.src-menu')).toBeVisible();
	const vocalOwn = page.locator('.src-row', { hasText: 'Vocal' }).locator('button', { hasText: 'OWN' });
	await expect(vocalOwn).toBeDisabled();
	await expect(vocalOwn).toHaveAttribute('title', /vocal lane has no serving implementation yet/);
});
