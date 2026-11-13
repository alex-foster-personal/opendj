// requirement: SEARCH-NAV-14416
/**
 * Whole-collection search failure at 200% browser zoom (issue #12949).
 */
import { expect, test, type Page } from '@playwright/test';

import { setBrowserZoom } from './support/browser-zoom';
import { spendBootLanding } from './support/boot-landing';

const SEARCH_QUERY = 'zoom-broken-search';
const ZOOM_FACTOR = 2;

/**
 * The app saves each tab's search text (the `library_tabs` pref) and all three
 * tests type one into the same engine, so a later test would open on the
 * earlier test's filter and find no rows. Start each test from no saved tabs.
 */
async function forgetSavedLibraryTabs(page: Page): Promise<void> {
	const reset = await page.request.put('/api/v1/ui-prefs', {
		data: { library_tabs: { tabs: [], active: 0 } }
	});
	expect(reset.ok(), `PUT ui-prefs: ${reset.status()} ${await reset.text()}`).toBeTruthy();
}

async function openLibraryWorkspace(page: Page): Promise<void> {
	await spendBootLanding(page);
	await forgetSavedLibraryTabs(page);
	await page.goto('/');
	await expect(page.getByTestId('library-workspace')).toBeVisible({ timeout: 30_000 });
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
}

async function armFailedWholeCollectionSearch(page: Page): Promise<void> {
	let calls = 0;
	await page.route('**/api/v1/search?**', async (route) => {
		calls += 1;
		if (calls === 1) {
			await route.abort('failed');
			return;
		}
		await route.continue();
	});
}

async function runWholeCollectionSearch(page: Page, query: string): Promise<void> {
	const search = page.locator('.rb-search input').first();
	await search.focus();
	await search.fill(query);
	const whole = page.locator('.whole-collection input[type="checkbox"]');
	await expect(whole).toBeVisible();
	await whole.check({ force: true });
	await expect(whole).toBeChecked();
	await expect(page.getByTestId('track-table-empty-message')).toBeVisible({ timeout: 15_000 });
}

function expectFullyInViewport(
	box: { x: number; y: number; width: number; height: number },
	viewport: { width: number; height: number }
): void {
	expect(box.x).toBeGreaterThanOrEqual(0);
	expect(box.y).toBeGreaterThanOrEqual(0);
	expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 0.5);
	expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 0.5);
}

test.beforeEach(async ({ page }) => {
	await openLibraryWorkspace(page);
	await armFailedWholeCollectionSearch(page);
	await runWholeCollectionSearch(page, SEARCH_QUERY);
	await setBrowserZoom(page, ZOOM_FACTOR);
});

test('[if] a search request fails at 200 percent zoom [then] the error message is visible', async ({
	page
}) => {
	const message = page.getByTestId('track-table-empty-message');
	await expect(message).toBeVisible({ timeout: 15_000 });
	await expect(message).toContainText(/search failed:/i);
	const box = await message.boundingBox();
	if (box === null) throw new Error('empty message has no bounding box');
	const viewport = page.viewportSize();
	if (viewport === null) throw new Error('viewportSize is null');
	expectFullyInViewport(box, viewport);
});

test('[if] the error message includes a retry action [then] retry is visible and enabled', async ({
	page
}) => {
	const retry = page.getByTestId('track-table-empty-retry');
	await expect(retry).toBeVisible({ timeout: 15_000 });
	await expect(retry).toHaveText('Retry search');
	await expect(retry).toBeEnabled();
	const box = await retry.boundingBox();
	if (box === null) throw new Error('retry control has no bounding box');
	const viewport = page.viewportSize();
	if (viewport === null) throw new Error('viewportSize is null');
	expectFullyInViewport(box, viewport);
	const hit = await retry.evaluate((el) => {
		const r = el.getBoundingClientRect();
		const target = document.elementFromPoint(
			Math.round(r.x + r.width / 2),
			Math.round(r.y + r.height / 2)
		);
		return target === el || (target !== null && el.contains(target));
	});
	expect(hit, 'retry must be hittable at its center at 200% zoom').toBe(true);
});

test('[if] the user has entered a search query before the failure [then] the query stays and failure copy shows', async ({
	page
}) => {
	const message = page.getByTestId('track-table-empty-message');
	await expect(message).toBeVisible({ timeout: 15_000 });
	await expect(message).toContainText(/search failed:/i);
	await expect(message).not.toContainText(/no tracks match/i);
	const search = page.locator('.rb-search input').first();
	await expect(search).toHaveValue(SEARCH_QUERY);
});
