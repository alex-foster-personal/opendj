/**
 * LIBUX-24 / pin 20cce2bab3d8: library row chrome on /performance.
 */
import { expect, test } from '@playwright/test';

test('performance: selected row highlight spans the library table-wrap width', async ({ page }) => {
	await page.setViewportSize({ width: 1280, height: 1000 });
	await page.goto('/performance');
	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible({ timeout: 60_000 });
	const row = page.locator('[data-testid="track-row"]').first();
	await expect(row).toBeVisible();
	await row.click();
	await expect(row).toHaveClass(/rb-row-selected/);

	const widths = await page.evaluate(() => {
		const wrap = document.querySelector('.table-wrap');
		const selected = document.querySelector('[data-testid="track-row"].rb-row-selected');
		if (!(wrap instanceof HTMLElement) || !(selected instanceof HTMLElement)) {
			return null;
		}
		return {
			wrap: wrap.clientWidth,
			row: selected.getBoundingClientRect().width
		};
	});
	expect(widths).not.toBeNull();
	// Row is as wide as the table (table may be wider than wrap when horizontally scrolled).
	expect(widths!.row).toBeGreaterThanOrEqual(widths!.wrap - 24);
});

test('performance: title text is vertically centered in the row and separator spans full row', async ({
	page
}) => {
	await page.setViewportSize({ width: 1280, height: 1000 });
	await page.goto('/performance');
	const row = page.locator('[data-testid="track-row"]').first();
	await expect(row).toBeVisible({ timeout: 60_000 });
	await row.click();

	const geometry = await page.evaluate(() => {
		const selected = document.querySelector('[data-testid="track-row"].rb-row-selected');
		const title = selected?.querySelector('.title-text');
		const art = selected?.querySelector('.c-art');
		if (!(selected instanceof HTMLElement) || !(title instanceof HTMLElement)) {
			return null;
		}
		const rowBox = selected.getBoundingClientRect();
		const titleBox = title.getBoundingClientRect();
		const rowCenterY = (rowBox.top + rowBox.bottom) / 2;
		const titleCenterY = (titleBox.top + titleBox.bottom) / 2;
		const artRight = art instanceof HTMLElement ? art.getBoundingClientRect().right : rowBox.left;
		return {
			titleCenterDelta: Math.abs(rowCenterY - titleCenterY),
			rowWidth: rowBox.width,
			separatorLeft: rowBox.left,
			artDoesNotCoverSeparator: artRight <= rowBox.left + 2
		};
	});
	expect(geometry).not.toBeNull();
	expect(geometry!.titleCenterDelta).toBeLessThan(4);
	expect(geometry!.rowWidth).toBeGreaterThan(200);
	expect(geometry!.artDoesNotCoverSeparator).toBe(true);
});
