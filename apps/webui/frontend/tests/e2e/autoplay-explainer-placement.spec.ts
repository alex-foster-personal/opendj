import { expect, test } from '@playwright/test';

const PANEL = '.ap-explain-panel';

test('AutoPlay explainer portals, remains interactive through its pointer corridor, and dismisses', async ({
	page
}) => {
	const pageErrors: string[] = [];
	page.on('pageerror', (error) => pageErrors.push(error.message));

	// No injected prefs: the shipped defaults (hide_broken_links false, auto_play_enabled true)
	// are what this scenario needs, and state must reach the page through the production path.
	await page.goto('/performance');
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });

	const trigger = page.getByLabel('AutoPlay order');
	await expect(trigger).toBeVisible();
	await trigger.hover();

	const panel = page.locator(PANEL);
	await expect(panel).toBeVisible();
	await expect(panel).toContainText('AutoPlay order');
	await expect(panel.locator("a[href*='Warnsdorff']")).toBeVisible();
	await expect(panel).toHaveCSS('position', 'fixed');
	expect(await panel.evaluate((element) => element.parentElement === document.body)).toBe(true);

	const panelBox = await panel.boundingBox();
	expect(panelBox, 'the visible panel must have browser geometry').not.toBeNull();
	expect(panelBox?.x).toBeGreaterThanOrEqual(0);
	expect(panelBox?.y).toBeGreaterThanOrEqual(0);
	expect((panelBox?.x ?? 0) + (panelBox?.width ?? 0)).toBeLessThanOrEqual(1280);
	expect((panelBox?.y ?? 0) + (panelBox?.height ?? 0)).toBeLessThanOrEqual(800);

	await panel.hover();
	await page.waitForTimeout(250);
	await expect(panel).toBeVisible();

	await page.mouse.move(0, 0);
	await expect(panel).toHaveCount(0, { timeout: 2_000 });
	expect(pageErrors).toEqual([]);
});
