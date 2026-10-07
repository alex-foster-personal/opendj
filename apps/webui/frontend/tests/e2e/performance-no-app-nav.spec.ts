// requirement: PERF-UI-07
// [if] /performance is showing [then] no bottom-left link strip is drawn,
// the browser's bottom bar starts at the window's left edge, and every page
// the strip used to link is still reachable through the mode picker and the
// app-shell sidebar's links (on the Crate Desk at /, LIBWS-01, its top bar's
// Pages menu carries them)

import { expect, test, type Page } from '@playwright/test';

/** The sidebar entries the removed strip duplicated, plus the ledger. */
const SIDEBAR_ROUTES = ['/reconcile', '/dedup', '/smartlists', '/admin', '/progress-tree'] as const;
/** Of those, the developer pages (lib/shell/sidebar-nav.ts `dev: true`): listed
 * only while the local "Show developer pages" pref (show_dev_ui) is on. */
const DEV_ROUTES: ReadonlySet<string> = new Set(['/admin', '/progress-tree']);

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
	// Admin and the ledger are developer pages (default hidden), so turn the
	// local pref on the way a developer would have it; the blob lives in the
	// rb-ui-prefs localStorage key (lib/rb/prefs.svelte.ts).
	await page.addInitScript(() => {
		const key = 'mdt.rb.ui-prefs.v1';
		const raw = window.localStorage.getItem(key);
		const blob = raw === null ? { hide_broken_links: false } : JSON.parse(raw);
		window.localStorage.setItem(key, JSON.stringify({ ...blob, show_dev_ui: true }));
	});
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	const picker = page.locator('details.mode-picker');
	await picker.locator('summary[aria-label="Choose app mode"]').click();
	await picker.locator('a.mode-card[data-testid="mode-card"]').filter({ hasText: 'Library' }).click();
	await page.waitForURL((url) => url.pathname === '/');

	// LIBWS-01: / is the full-bleed Crate Desk, whose top bar's Pages menu
	// renders the sidebar's own link list.
	const pages = page.getByTestId('library-pages-menu');
	await pages.locator('summary').click();
	for (const route of SIDEBAR_ROUTES) {
		await expect(pages.locator(`a[href="${route}"]`), `pages link ${route}`).toBeVisible();
	}
	await pages.locator('a[href="/admin"]').click();
	await expect(page).toHaveURL(/\/admin/);
});

test('developer pages stay out of the Crate Desk Pages menu while the pref is off', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const picker = page.locator('details.mode-picker');
	await picker.locator('summary[aria-label="Choose app mode"]').click();
	await picker.locator('a.mode-card[data-testid="mode-card"]').filter({ hasText: 'Library' }).click();
	await page.waitForURL((url) => url.pathname === '/');

	const pages = page.getByTestId('library-pages-menu');
	await pages.locator('summary').click();
	for (const route of SIDEBAR_ROUTES) {
		const link = pages.locator(`a[href="${route}"]`);
		if (DEV_ROUTES.has(route)) await expect(link, `dev link ${route}`).toHaveCount(0);
		else await expect(link, `pages link ${route}`).toBeVisible();
	}
});
