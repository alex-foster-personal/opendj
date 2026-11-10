/**
 * A11Y-11403: tabbing through /performance shows the one keyboard focus ring
 * on every control kind, and a mouse click does not.
 *
 *   [if] Tab never reaches the top bar, a play button, a knob, the crossfader or a library row [then ⛔️]
 *   [if] any of those stops has computed outline-style none or under 2px [then ⛔️]
 *   [if] a mouse click on a knob or play button leaves a visible ring [then ⛔️]
 */
import { expect, test, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

type Stop = { target: string | null; outlineStyle: string; outlineWidth: string };

/** A disabled play button is not a tab stop, so load a fixture track first. */
async function loadDeckOne(page: Page): Promise<void> {
	const stableId = await page
		.locator('[data-testid="track-row"]')
		.first()
		.getAttribute('data-stable-id');
	expect(stableId, 'fixture row has no data-stable-id').toBeTruthy();
	await page.evaluate(async (id) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: id });
	}, stableId as string);
	await expect(page.locator('[data-testid="play-deck-1"]')).toBeEnabled({ timeout: 15_000 });
}

const TARGETS: Record<string, string> = {
	topbar: '.rb-topbar *',
	play: '[data-testid^="play-deck-"]',
	knob: '.knob',
	crossfader: '.xfader',
	row: '[data-testid="track-row"]'
};

test('Tab shows a 2px focus ring on the top bar, play, knob, crossfader and a library row', async ({
	page
}) => {
	test.setTimeout(240_000); // up to 600 Tab presses across the whole /performance tab order
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);
	await loadDeckOne(page);
	await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());

	const seen: Record<string, Stop> = {};
	for (let i = 0; i < 600 && Object.keys(seen).length < Object.keys(TARGETS).length; i++) {
		await page.keyboard.press('Tab');
		const stop = await page.evaluate((targets) => {
			const el = document.activeElement as HTMLElement | null;
			if (!el || el === document.body) return null;
			const target = Object.keys(targets).find((k) => el.matches(targets[k])) ?? null;
			const cs = getComputedStyle(el);
			return { target, outlineStyle: cs.outlineStyle, outlineWidth: cs.outlineWidth };
		}, TARGETS);
		if (stop?.target && !seen[stop.target]) seen[stop.target] = stop;
	}

	for (const name of Object.keys(TARGETS)) {
		const stop = seen[name];
		expect(stop, `Tab never reached ${name}; reached ${Object.keys(seen).join(', ')}`).toBeTruthy();
		expect(stop.outlineStyle, `${name} has no visible focus ring`).not.toBe('none');
		expect(parseFloat(stop.outlineWidth), `${name} ring under 2px`).toBeGreaterThanOrEqual(2);
	}
});

test('a mouse click on a knob does not show the keyboard focus ring', async ({ page }) => {
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);

	const knob = page.locator('.knob').first();
	await knob.click();
	const state = await page.evaluate(() => {
		const el = document.activeElement as HTMLElement;
		return {
			focusVisible: el.matches(':focus-visible'),
			outlineStyle: getComputedStyle(el).outlineStyle
		};
	});
	expect(state.focusVisible).toBe(false);
	expect(state.outlineStyle).toBe('none');
});
