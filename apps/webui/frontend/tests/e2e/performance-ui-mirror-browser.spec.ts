import { expect, test } from '@playwright/test';

interface BrowserMirror {
	search: string | null;
	sort: { key: string; direction: 'asc' | 'desc' } | null;
	selected_row: string | null;
}

async function fetchBrowserMirror(page: import('@playwright/test').Page): Promise<BrowserMirror | null> {
	return page.evaluate(async () => {
		const response = await fetch('/api/v1/state/ui-mirror');
		if (!response.ok) return null;
		const mirror = await response.json();
		const browser = mirror.browser;
		if (browser === null || typeof browser !== 'object') return null;
		return {
			search: browser.search ?? null,
			sort: browser.sort ?? null,
			selected_row: browser.selected_row ?? null
		};
	});
}

test('ui-mirror publishes live browser search, sort, and selected row', async ({ page }) => {
	test.setTimeout(120_000);

	await page.goto('/performance', { waitUntil: 'domcontentloaded' });
	await expect
		.poll(() => page.evaluate(() => window.musicDjToolsPerformance?.version ?? null), { timeout: 60_000 })
		.toBe(1);

	await page.getByRole('button', { name: /all tracks/i }).click();
	const trackRow = page.locator('[data-testid="track-row"][data-stable-id]').first();
	await expect(trackRow).toBeVisible({ timeout: 60_000 });
	const stableId = await trackRow.getAttribute('data-stable-id');
	expect(stableId, 'fixture library must expose a track row').toBeTruthy();

	await expect.poll(() => fetchBrowserMirror(page), { timeout: 30_000 }).toMatchObject({
		search: null,
		sort: null,
		selected_row: null
	});

	const searchText = 'mirror-probe';
	const search = page.locator('.rb-search input');
	await search.fill(searchText);
	await expect.poll(() => fetchBrowserMirror(page), { timeout: 15_000 }).toMatchObject({
		search: searchText
	});

	await search.fill('');
	await expect.poll(() => fetchBrowserMirror(page), { timeout: 15_000 }).toMatchObject({
		search: null
	});

	const titleHeader = page.locator('th.h-title.sortable');
	await titleHeader.click();
	await expect.poll(() => fetchBrowserMirror(page), { timeout: 15_000 }).toMatchObject({
		sort: { key: 'title', direction: 'asc' }
	});

	await titleHeader.click();
	await expect.poll(() => fetchBrowserMirror(page), { timeout: 15_000 }).toMatchObject({
		sort: { key: 'title', direction: 'desc' }
	});

	const rowToSelect = page.locator(`[data-testid="track-row"][data-stable-id="${stableId}"]`);
	await expect(rowToSelect).toBeVisible({ timeout: 15_000 });
	await rowToSelect.click();
	await expect.poll(() => fetchBrowserMirror(page), { timeout: 15_000 }).toMatchObject({
		selected_row: stableId
	});
});
