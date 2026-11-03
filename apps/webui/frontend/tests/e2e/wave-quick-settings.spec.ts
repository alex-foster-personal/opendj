/**
 * requirement: WAVEQS-8348c, WAVEQS-8348d
 *
 * The top bar's waveform glyph opens the quick-settings popover, a second view
 * over the Settings catalog (issue #8343).
 *
 * Regression lines:
 * - if a click on the glyph does not open a labeled dialog with focus inside then broken
 * - if flipping Show sections in the popover does not reach the disk pref and the Settings row then broken
 * - if Escape or a pointer press outside leaves the popover open then broken
 * - if a second click while the chunk loads leaves the popover open then broken
 */
import { expect, test, type Page } from '@playwright/test';

const BUTTON = '[data-testid="wave-quick-settings-button"]';
const DIALOG = 'role=dialog[name="Waveform quick settings"]';

async function openPerformance(page: Page): Promise<void> {
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect(page.locator(BUTTON)).toBeVisible({ timeout: 30_000 });
}

async function diskShowSections(page: Page): Promise<boolean> {
	return page.evaluate(async () => {
		const body = (await (await fetch('/api/v1/ui-prefs')).json()) as { show_sections: boolean };
		return body.show_sections;
	});
}

test('WAVEQS-8348c/8348d: the popover opens, flips sections for Settings and disk, and closes on Escape and outside press', async ({ page }) => {
	test.setTimeout(120_000);
	await openPerformance(page);

	await page.locator(BUTTON).click();
	const dialog = page.locator(DIALOG);
	await expect(dialog).toBeVisible();
	await expect(page.locator(BUTTON)).toHaveAttribute('aria-expanded', 'true');
	await expect.poll(() => dialog.evaluate((el) => el.contains(document.activeElement))).toBe(true);

	const sections = dialog.locator('#wqs-show_sections');
	const before = await sections.isChecked();
	await sections.click();
	await expect(sections).toBeChecked({ checked: !before });
	await expect.poll(() => diskShowSections(page), { timeout: 10_000 }).toBe(!before);

	await page.keyboard.press('Escape');
	await expect(dialog).toHaveCount(0);
	await expect(page.locator(BUTTON)).toBeFocused();

	// The Settings overlay shows the value the popover wrote (one store, two views).
	await page.keyboard.press('ControlOrMeta+Comma');
	await page.locator('.so-search').fill('Show sections');
	await expect(page.locator('input[aria-label="Show sections"]')).toBeChecked({ checked: !before });
	await page.keyboard.press('Escape');

	await page.locator(BUTTON).click();
	await expect(dialog).toBeVisible();
	await page.mouse.click(40, 600);
	await expect(dialog).toHaveCount(0);

	// Restore the fixture's pref so the next spec starts from the default.
	await page.locator(BUTTON).click();
	await dialog.locator('#wqs-show_sections').click();
	await expect.poll(() => diskShowSections(page), { timeout: 10_000 }).toBe(before);
});

test('WAVEQS-8348d: a second click while the popover chunk loads closes it', async ({ page }) => {
	await openPerformance(page);
	const expanded = await page.locator(BUTTON).evaluate((btn) => {
		(btn as HTMLButtonElement).click();
		(btn as HTMLButtonElement).click();
		return btn.getAttribute('aria-expanded');
	});
	expect(expanded).toBe('false');
	await page.waitForTimeout(1500);
	await expect(page.locator(DIALOG)).toHaveCount(0);
});
