/**
 * Issue #10364: overlay focus trap and focus return to opener.
 *
 * [if] Settings opened with Cmd/Ctrl+, from a focused deck play button and
 * closed with Esc [then] document.activeElement is that play button [else stop].
 * [if] Hotkeys opened with ? from a focused library row and closed with X
 * [then] document.activeElement is that row [else stop].
 */
import { expect, test, type ElementHandle, type Locator, type Page } from '@playwright/test';

import { FOCUSABLE_SELECTOR } from '../../src/lib/keyboard/focus-trap';
import { waitForPerformanceIpc } from './support/performance-ready';

async function isActiveElement(
	page: Page,
	handle: ElementHandle<SVGElement | HTMLElement> | null
): Promise<boolean> {
	if (!handle) return false;
	return page.evaluate((el) => document.activeElement === el, handle);
}

async function activeIsLastTabStop(dialog: Locator): Promise<boolean> {
	return dialog.evaluate((root, selector) => {
		const nodes = [...root.querySelectorAll<HTMLElement>(selector)];
		let last: HTMLElement | null = null;
		for (const el of nodes) {
			if (el.matches(':disabled')) continue;
			if (el.tabIndex < 0) continue;
			if (el.closest('[aria-hidden="true"], [inert]')) continue;
			const rendered =
				typeof el.checkVisibility === 'function'
					? el.checkVisibility({ visibilityProperty: true })
					: el.getClientRects().length > 0;
			if (!rendered) continue;
			last = el;
		}
		return last !== null && document.activeElement === last;
	}, FOCUSABLE_SELECTOR);
}

test('Settings Tab wraps and Esc returns focus to deck play', async ({ page }) => {
	await page.goto('/performance');
	const rows = page.locator('[data-testid="track-row"]');
	await expect(rows.first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);

	const play = page.locator('button[aria-label="play deck 1"]').first();
	await play.focus();
	const playHandle = await play.elementHandle();
	expect(await isActiveElement(page, playHandle)).toBe(true);

	const dialog = page.getByRole('dialog', { name: 'Settings' });
	await expect(async () => {
		if (!(await dialog.isVisible())) await page.keyboard.press('ControlOrMeta+Comma');
		await expect(dialog).toBeVisible();
	}).toPass({ timeout: 45_000 });

	const search = page.getByPlaceholder('Search settings...');
	await expect(search).toBeFocused();

	for (let i = 0; i < 80; i++) {
		if (await activeIsLastTabStop(dialog)) break;
		await page.keyboard.press('Tab');
		await expect
			.poll(async () => dialog.evaluate((d) => d.contains(document.activeElement)))
			.toBe(true);
	}

	await page.keyboard.press('Tab');
	await expect(search).toBeFocused();

	await page.keyboard.press('Shift+Tab');
	await expect.poll(() => activeIsLastTabStop(dialog)).toBe(true);

	await page.keyboard.press('Escape');
	await expect(dialog).toBeHidden();
	expect(await isActiveElement(page, playHandle)).toBe(true);
});

test('Hotkeys Shift+Tab to close and focus returns to library row', async ({ page }) => {
	await page.goto('/performance');
	const rows = page.locator('[data-testid="track-row"]');
	await expect(rows.first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);

	const row = rows.first();
	await row.focus();
	const rowHandle = await row.elementHandle();
	expect(await isActiveElement(page, rowHandle)).toBe(true);

	const dialog = page.getByRole('dialog', { name: 'Hotkeys' });
	await expect(async () => {
		if (!(await dialog.isVisible())) await page.keyboard.press('?');
		await expect(dialog).toBeVisible();
	}).toPass({ timeout: 45_000 });

	await expect(page.getByLabel('Filter hotkeys')).toBeFocused();

	await page.keyboard.press('Shift+Tab');
	await expect(page.getByRole('button', { name: 'Close hotkeys overlay' })).toBeFocused();

	await page.getByRole('button', { name: 'Close hotkeys overlay' }).click();
	await expect(dialog).toBeHidden();
	expect(await isActiveElement(page, rowHandle)).toBe(true);
});
