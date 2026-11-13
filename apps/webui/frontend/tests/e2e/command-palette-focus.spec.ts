/**
 * Issue #12947: keyboard focus returns to the invoking control after the v2
 * command palette closes without picking a result.
 */
import { expect, test, type ElementHandle, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

const NO_POSIX_LAUNCHER = process.platform === 'win32';

async function isActiveElement(
	page: Page,
	handle: ElementHandle<SVGElement | HTMLElement> | null
): Promise<boolean> {
	if (!handle) return false;
	return page.evaluate((el) => document.activeElement === el, handle);
}

async function readyOnPerformance(
	page: Page
): Promise<{ openerHandle: ElementHandle<SVGElement | HTMLElement> }> {
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);
	const opener = page.locator('input[aria-label="text command entry"]');
	await expect(opener).toBeVisible();
	await opener.focus();
	const openerHandle = await opener.elementHandle();
	if (openerHandle === null) throw new Error('command entry handle missing');
	await expect.poll(async () => isActiveElement(page, openerHandle)).toBe(true);
	return { openerHandle };
}

async function openV2Bar(page: Page): Promise<void> {
	await expect(async () => {
		await page.keyboard.press('Control+k');
		await expect(page.locator('#cmdk2-list')).toBeVisible({ timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	await expect(page.getByTestId('command-bar-input')).toBeFocused();
}

test.skip(NO_POSIX_LAUNCHER, 'the e2e launcher is POSIX-only today');

test('[if] a keyboard user opens the command palette [then] the search input is focused', async ({
	page
}) => {
	await readyOnPerformance(page);
	await openV2Bar(page);
});

test('[if] a keyboard user closes the command palette without selecting an action [then] the bar is hidden', async ({
	page
}) => {
	await readyOnPerformance(page);
	await openV2Bar(page);
	const bar = page.getByTestId('command-bar');
	await page.keyboard.press('Control+k');
	await expect(bar).toBeHidden({ timeout: 5_000 });
});

test('[if] a keyboard user then presses Tab [then] focus advances from the invoking control', async ({
	page
}) => {
	const { openerHandle } = await readyOnPerformance(page);
	await openV2Bar(page);
	const bar = page.getByTestId('command-bar');
	await page.keyboard.press('Control+k');
	await expect(bar).toBeHidden({ timeout: 5_000 });
	expect(await isActiveElement(page, openerHandle)).toBe(true);
	await page.keyboard.press('Tab');
	expect(await isActiveElement(page, openerHandle)).toBe(false);
	await expect
		.poll(async () => page.evaluate(() => document.activeElement?.tagName ?? ''))
		.not.toBe('BODY');
});
