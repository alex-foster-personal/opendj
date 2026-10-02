/**
 * Real-browser coverage for library track-list keyboard navigation
 * (TrackTable + track-table-keyboard.ts). The root Playwright config starts a
 * real fixture engine and Vite, so these keys drive the production selection
 * model and the production double-click load path.
 *
 * Acceptance:
 * - [if] ArrowDown/ArrowUp do not move the selection and DOM focus [then stop]
 * - [if] Shift+ArrowUp does not extend a range like Shift+click [then stop]
 * - [if] Home/End do not jump to the first/last row [then stop]
 * - [if] arrows typed in the search box move the list [then stop]
 * - [if] Enter does not open the same load+play confirm as a double-click [then stop]
 * - [if] the table is not a grid with aria-selected rows and one Tab stop [then stop]
 */
import { expect, test } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

test('library track list is keyboard navigable and announces its selection', async ({ page }) => {
	await page.goto('/performance');
	const rows = page.locator('[data-testid="track-row"]');
	await expect(rows.first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);

	const table = page.locator('[data-testid="track-table"]');
	await expect(table).toHaveAttribute('role', 'grid');
	await expect(table).toHaveAttribute('aria-multiselectable', 'true');
	const count = await rows.count();
	expect(count, 'fixture library needs at least two rows').toBeGreaterThanOrEqual(2);
	await expect(table).toHaveAttribute('aria-rowcount', String(count + 1));

	const first = rows.nth(0);
	const second = rows.nth(1);
	const last = rows.nth(count - 1);

	await first.click();
	await expect(first).toHaveAttribute('aria-selected', 'true');
	await expect(first).toHaveAttribute('aria-rowindex', '2');
	// Roving tabindex: exactly one row is the Tab stop.
	await expect(page.locator('[data-testid="track-row"][tabindex="0"]')).toHaveCount(1);
	await first.focus();

	await page.keyboard.press('ArrowDown');
	await expect(second).toHaveAttribute('aria-selected', 'true');
	await expect(first).toHaveAttribute('aria-selected', 'false');
	await expect(second).toBeFocused();
	await expect(second).toHaveAttribute('tabindex', '0');

	await page.keyboard.press('Shift+ArrowUp');
	await expect(first).toHaveAttribute('aria-selected', 'true');
	await expect(second).toHaveAttribute('aria-selected', 'true');
	await expect(first).toBeFocused();

	await page.keyboard.press('End');
	await expect(last).toHaveAttribute('aria-selected', 'true');
	await expect(first).toHaveAttribute('aria-selected', count === 1 ? 'true' : 'false');
	await expect(last).toBeFocused();

	await page.keyboard.press('Home');
	await expect(first).toHaveAttribute('aria-selected', 'true');
	await expect(last).toHaveAttribute('aria-selected', 'false');
	await expect(first).toBeFocused();

	// Typing in the search box never moves the list.
	const search = page.locator('.rb-search input').first();
	await search.focus();
	await page.keyboard.press('ArrowDown');
	await page.keyboard.press('End');
	await expect(first).toHaveAttribute('aria-selected', 'true');
	await expect(search).toBeFocused();

	// Enter takes the double-click load path: same confirm, Yes focused.
	await first.focus();
	await page.keyboard.press('Enter');
	const confirm = page.locator('.load-confirm[role="dialog"]');
	await expect(confirm).toBeVisible();
	await expect(confirm).toContainText(/Load\+play CH\d/);
	await expect(confirm.locator('.load-confirm-yes')).toBeFocused();
	await confirm.locator('.load-confirm-no').click();
	await expect(confirm).toHaveCount(0);
});
