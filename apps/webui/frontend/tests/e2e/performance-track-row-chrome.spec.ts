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

	// Separator geometry uses tbody tr::after (left:0; right:0 on the row). Pseudo-elements
	// have no getBoundingClientRect in Playwright, so read computed left/right/width and
	// compare to the row box. Artwork must be a real column (nonzero width) so this cannot
	// pass vacuously when .c-art is missing.
	const geometry = await page.evaluate(() => {
		const selected = document.querySelector('[data-testid="track-row"].rb-row-selected');
		const nextRow = selected?.nextElementSibling;
		const titleCell = selected?.querySelector('.c-title');
		const artistCell = selected?.querySelector('.c-artist');
		const title = selected?.querySelector('.title-text');
		const art = selected?.querySelector('.c-art');
		const artImg = selected?.querySelector('.c-art img');
		const nextTitle = nextRow?.querySelector('.title-text');
		if (!(selected instanceof HTMLElement) || !(title instanceof HTMLElement)) {
			return null;
		}
		if (!(titleCell instanceof HTMLElement) || !(artistCell instanceof HTMLElement)) {
			return { error: 'missing title or artist table cell' as const };
		}
		const titleCellDisplay = window.getComputedStyle(titleCell).display;
		const artistCellDisplay = window.getComputedStyle(artistCell).display;
		const rowBox = selected.getBoundingClientRect();
		const titleBox = title.getBoundingClientRect();
		const rowCenterY = (rowBox.top + rowBox.bottom) / 2;
		const titleCenterY = (titleBox.top + titleBox.bottom) / 2;

		const artEl = artImg instanceof HTMLElement ? artImg : art;
		if (!(artEl instanceof HTMLElement)) {
			return { error: 'missing artwork column' as const };
		}
		const artBox = artEl.getBoundingClientRect();
		if (artBox.width < 4) {
			return { error: 'artwork column has zero rendered width' as const };
		}

		const afterStyle = window.getComputedStyle(selected, '::after');
		const afterLeft = parseFloat(afterStyle.left);
		const afterRight = parseFloat(afterStyle.right);
		const afterWidth = parseFloat(afterStyle.width);
		const separatorSpanPx =
			Number.isFinite(afterWidth) && afterWidth > 0
				? afterWidth
				: rowBox.width - afterLeft - afterRight;
		const separatorY = rowBox.bottom - 0.5;

		let separatorBalanceDelta: number | null = null;
		if (nextRow instanceof HTMLElement && nextTitle instanceof HTMLElement) {
			const nextTitleBox = nextTitle.getBoundingClientRect();
			const nextTitleCenterY = (nextTitleBox.top + nextTitleBox.bottom) / 2;
			const gapAbove = separatorY - titleCenterY;
			const gapBelow = nextTitleCenterY - separatorY;
			separatorBalanceDelta = Math.abs(gapAbove - gapBelow);
		}

		return {
			titleCellDisplay,
			artistCellDisplay,
			titleCenterDelta: Math.abs(rowCenterY - titleCenterY),
			rowWidth: rowBox.width,
			separatorSpanPx,
			separatorSpanDelta: Math.abs(separatorSpanPx - rowBox.width),
			separatorBalanceDelta,
			artWidth: artBox.width
		};
	});
	expect(geometry).not.toBeNull();
	if (geometry && 'error' in geometry) {
		throw new Error(`row chrome precondition failed: ${geometry.error}`);
	}
	expect(geometry!.titleCellDisplay).toBe('table-cell');
	expect(geometry!.artistCellDisplay).toBe('table-cell');
	expect(geometry!.titleCenterDelta).toBeLessThan(4);
	expect(geometry!.rowWidth).toBeGreaterThan(200);
	expect(geometry!.artWidth).toBeGreaterThan(4);
	expect(geometry!.separatorSpanDelta).toBeLessThan(2);
	expect(geometry!.separatorBalanceDelta).not.toBeNull();
	expect(geometry!.separatorBalanceDelta!).toBeLessThanOrEqual(1);
});
