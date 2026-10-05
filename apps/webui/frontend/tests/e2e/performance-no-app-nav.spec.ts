// requirement: PERF-UI-07
// [if] /performance is showing [then] no bottom-left link strip is drawn,
// the browser's bottom bar starts at the window's left edge, and every page
// the strip used to link is still reachable through the mode picker and the
// app-shell sidebar

import { expect, test, type Page } from '@playwright/test';

/** The sidebar entries the removed strip duplicated, plus the ledger. */
const SIDEBAR_ROUTES = ['/reconcile', '/dedup', '/smartlists', '/admin', '/progress-tree'] as const;

async function expectNoLinkStrip(page: Page): Promise<void> {
	// Presence first: the performance surface really is on screen, so the
	// absence below is a statement about a rendered page and not a blank one.
	await expect(page.locator('.perf-root .bottom-bar')).toBeVisible();
	await expect(page.locator('[data-testid="performance-app-nav"]')).toHaveCount(0);
	await expect(page.locator('nav[aria-label="App navigation"]')).toHaveCount(0);
}

test('performance draws no bottom-left link strip and the bottom bar starts at the left edge', async ({
	page
}) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expectNoLinkStrip(page);

	// Nothing fixed sits over the bar's left end: the topmost element there
	// belongs to the bar itself.
	const owner = await page.evaluate(() => {
		const bar = document.querySelector('.perf-root .bottom-bar');
		if (bar === null) throw new Error('.bottom-bar not found');
		const box = bar.getBoundingClientRect();
		const top = document.elementFromPoint(box.left + 3, box.top + box.height / 2);
		return { insideBar: top !== null && bar.contains(top), tag: top?.tagName ?? 'null' };
	});
	expect(owner.insideBar, `topmost element at the bar's left end is ${owner.tag}`).toBe(true);

	// The bar no longer reserves the strip's 232px: its first child begins
	// within the bar's own side padding.
	const inset = await page.evaluate(() => {
		const bar = document.querySelector('.perf-root .bottom-bar');
		if (bar === null) throw new Error('.bottom-bar not found');
		return {
			paddingLeft: Number.parseFloat(getComputedStyle(bar).paddingLeft),
			paddingRight: Number.parseFloat(getComputedStyle(bar).paddingRight)
		};
	});
	expect(inset.paddingLeft, 'bottom bar left inset').toBe(inset.paddingRight);
});

test('performance preload route draws no link strip either', async ({ page }) => {
	await page.goto('/performance/preload1?muted=1', { waitUntil: 'domcontentloaded' });
	await expectNoLinkStrip(page);
});

test('every page the strip linked is reachable from performance without a typed URL', async ({
	page
}) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	const picker = page.locator('details.mode-picker');
	await picker.locator('summary[aria-label="Choose app mode"]').click();
	await picker.locator('a.mode-card[data-testid="mode-card"]').filter({ hasText: 'Library' }).click();
	await page.waitForURL((url) => url.pathname === '/');

	for (const route of SIDEBAR_ROUTES) {
		await expect(page.locator(`aside.sidebar a[href="${route}"]`), `sidebar link ${route}`).toBeVisible();
	}
	await page.locator('aside.sidebar a[href="/admin"]').click();
	await expect(page).toHaveURL(/\/admin/);
});
