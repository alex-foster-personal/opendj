// requirement: UX-EXPLAIN-02
// [if] hover performance explainers [then] popover teaches split/LINK/MIX/mode, else stop
import { expect, test } from '@playwright/test';
import { waitForPerformanceIpc } from './support/performance-ready';

async function expectExplainerPop(page: import('@playwright/test').Page, trigger: string, pattern: RegExp): Promise<void> {
	const control = page.locator(trigger).first();
	await control.hover();
	await expect(page.locator('.explainer .pop').filter({ has: page.locator('.head') }).first()).toBeVisible({
		timeout: 2500
	});
	await expect(page.locator('.explainer .pop .head').first()).toContainText(pattern);
}

test('issue 3982: performance explainers and vibe thumb colors', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);

	await expectExplainerPop(page, 'button[aria-label="split view"]', /split/i);
	await page.mouse.move(0, 0);
	await expectExplainerPop(page, 'button.link-btn[aria-label="LINK"]', /LINK/i);
	await page.mouse.move(0, 0);
	await expectExplainerPop(page, '[aria-label="Headphone CUE to MASTER mix"]', /MIX/i);
	await page.mouse.move(0, 0);
	await expectExplainerPop(page, '.hp-mode', /practice|split|output/i);

	const upColor = await page.locator('button[aria-label="thumbs up"]').evaluate((el) => getComputedStyle(el).color);
	const downColor = await page.locator('button[aria-label="thumbs down"]').evaluate((el) => getComputedStyle(el).color);
	const parseRgb = (css: string): number[] => {
		const m = css.match(/rgba?\(([^)]+)\)/);
		if (m === null) return [];
		return m[1].split(',').map((v) => Number.parseFloat(v.trim()));
	};
	const [ug, , ub] = parseRgb(upColor);
	const [dr] = parseRgb(downColor);
	expect(ug).toBeGreaterThan(100);
	expect(ub).toBeLessThan(120);
	expect(dr).toBeGreaterThan(80);
});
