/**
 * Regression for #8887: a real primary press on a column chooser row must run
 * prefs.toggleColumn. Rows use `pointerdowncapture` + `preventDefault` so
 * activation runs before bubble `pointerdown` on `window` dismisses outside (#8887).
 *
 * Acceptance:
 * - [if] right-clicking the track table header shows no chooser [then stop]
 * - [if] a real primary press on Plays leaves `th.h-plays` visible and `hide-plays` off the table [then stop]
 * - [if] `HTMLElement.click()` on the item is the only press that hides the column [then stop]
 * - [if] Enter on the focused Plays row no longer toggles [then stop]
 * - [if] an outside pointerdown leaves the chooser visible [then stop]
 */
import { expect, test, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

const TABLE = '[data-testid="track-table"]';
const MENU = '[data-testid="context-menu"]';
const PLAYS_CHOOSER = '[data-testid="column-chooser-plays"]';

async function openColumnChooserFrom(page: Page, headerSelector: string): Promise<void> {
	await page.locator(headerSelector).click({ button: 'right' });
	await expect(page.locator(PLAYS_CHOOSER)).toBeVisible();
}

test('column chooser: real pointer toggles Plays and Enter still works', async ({ page }) => {
	test.setTimeout(60_000);
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);

	const thead = page.locator(`${TABLE} thead`);
	await thead.scrollIntoViewIfNeeded();
	const playsHeader = page.locator(`${TABLE} thead th.h-plays`);
	// After Plays is hidden, `th.h-plays` is not hit-testable; use another header (chooser opens from `thead`).
	const otherHeader = page.locator(`${TABLE} thead th.h-bpm`);

	await openColumnChooserFrom(page, `${TABLE} thead th.h-plays`);

	const probe = await page.locator(PLAYS_CHOOSER).evaluate((el) => {
		const rect = el.getBoundingClientRect();
		const x = rect.x + rect.width / 2;
		const y = rect.y + rect.height / 2;
		const resolved = document.elementFromPoint(x, y);
		const hitButton = el === resolved || (resolved instanceof Node && el.contains(resolved));
		return {
			x,
			y,
			hitButton,
			tag: resolved instanceof Element ? resolved.tagName : null,
			testId: resolved instanceof Element ? resolved.getAttribute('data-testid') : null
		};
	});
	expect(
		probe.hitButton,
		`expected Plays chooser at center; got ${probe.tag} testId=${probe.testId}`
	).toBe(true);

	await page.mouse.click(probe.x, probe.y);
	await expect(page.locator(TABLE)).toHaveClass(/hide-plays/);
	await expect(playsHeader).toBeHidden();

	await openColumnChooserFrom(page, `${TABLE} thead th.h-bpm`);
	const probe2 = await page.locator(PLAYS_CHOOSER).evaluate((el) => {
		const rect = el.getBoundingClientRect();
		return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
	});
	await page.mouse.click(probe2.x, probe2.y);
	await expect(page.locator(TABLE)).not.toHaveClass(/hide-plays/);
	await expect(playsHeader).toBeVisible();

	// Keyboard: focused row still toggles (native button activation).
	await openColumnChooserFrom(page, `${TABLE} thead th.h-plays`);
	await page.locator(PLAYS_CHOOSER).focus();
	await page.keyboard.press('Enter');
	await expect(page.locator(TABLE)).toHaveClass(/hide-plays/);
	await expect(playsHeader).toBeHidden();

	// Outside dismiss: primary pointerdown outside the menu closes it.
	await openColumnChooserFrom(page, `${TABLE} thead th.h-bpm`);
	await page.mouse.move(1, 1);
	await page.mouse.down();
	await expect(page.locator(MENU)).toHaveCount(0);
	await page.mouse.up();
	await expect(page.locator(TABLE)).toHaveClass(/hide-plays/);

	await expect(otherHeader).toBeVisible();
});
