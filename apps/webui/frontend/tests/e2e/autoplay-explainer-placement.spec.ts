import { expect, test } from '@playwright/test';

const PANEL = '.ap-explain-panel';

test('AutoPlay explainer portals, remains interactive through its pointer corridor, and dismisses', async ({
	page
}) => {
	const pageErrors: string[] = [];
	page.on('pageerror', (error) => pageErrors.push(error.message));

	// pin 246b0f5 follow-up (fix round 2): this spec's own geometry
	// assertions below already assumed a 1280x800 viewport
	// (`toBeLessThanOrEqual(800)`), but the root suite's project sets no
	// viewport at all, so Playwright's Desktop Chrome default (1280x720)
	// applied instead - 80px short of what every other /performance-aware
	// config in this repo (performance/desktop-setup/play-analytics/stems/
	// savepoint) explicitly requests, and short of the 969px
	// deck-loader-placement.spec.ts documents as the threshold below which
	// the deck-area's own overflow already stole hit-testing from the
	// library before this pin ever touched the floor. Pin 246b0f5's MORE
	// floor growth (497px -> 524px) tipped that latent 720px shortfall from
	// "barely fits" into "the AutoPlay-order header and the library rows
	// collide with .bottom-bar/deck-area stems", which is what timed out
	// here. Setting the viewport explicitly (matching deck-loader-placement
	// .spec.ts's own precedent for the identical failure class) is the
	// layout fix: it makes this spec run at the height its own assertions
	// already assumed, not a weakened assertion or a longer timeout.
	await page.setViewportSize({ width: 1280, height: 800 });
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
	// PLAY-05: the panel IS the queue viewer, so the published-plan section has
	// to be part of what opens - not just a count in the header tooltip. The
	// trigger above is located by the header's aria-label, which is why that
	// label stays 'AutoPlay order' while the tooltip talks about the queue.
	await expect(panel).toContainText('Published queue');
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
