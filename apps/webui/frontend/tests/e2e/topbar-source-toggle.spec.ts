/**
 * PARITY-02 (issue #1002): the SOURCE toggle must be usable with a mouse and
 * must not crush the command entry out of the responsive top bar.
 *
 * Regression lines:
 * - if a pointer moving from the SOURCE button down into the menu closes the
 *   menu before it arrives then broken (discussion_r3968534392)
 * - if the command entry is laid out at some viewport width but its input is
 *   not hittable there then broken (discussion_r3968534403)
 *
 * The width test pins an INVARIANT, not the measured numbers in TopBar.svelte's
 * comment blocks: at every width, the command entry is either evicted (a
 * deliberate, honest state the row already uses below 826px) or hittable.
 * "Crushed" - laid out, visible, zero effective hit area - is the state that is
 * never allowed, because it looks operable and is not. Breakpoints can move
 * without this test rotting; a control that silently stops taking clicks
 * cannot.
 *
 * Hit-tested with a real elementFromPoint rather than offsetWidth: near a crush
 * boundary offsetWidth predicts hittability non-monotonically.
 */
import { expect, test, type Page } from '@playwright/test';

/** Spans every breakpoint this row has (825, 980, 1180, 1210, 1400, 1530) with
 * room either side, at the same 5px granularity the comment blocks were
 * measured at. */
const FROM = 790;
const TO = 1920;
const STEP = 5;

type Probe = { evicted: boolean; hittable: boolean };

async function _probeCommandEntry(page: Page): Promise<Probe> {
	return page.evaluate(() => {
		const input = document.querySelector('input[aria-label="text command entry"]');
		if (input === null) return { evicted: true, hittable: false };
		const rect = input.getBoundingClientRect();
		if (rect.width === 0 || rect.height === 0) return { evicted: true, hittable: false };
		const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
		return { evicted: false, hittable: hit === input || input.contains(hit) };
	});
}

async function _openPerformance(page: Page): Promise<void> {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

test('the command entry is never crushed at any top bar width', async ({ page }) => {
	test.setTimeout(180_000);
	await _openPerformance(page);
	await expect(page.locator('.src-toggle')).toBeVisible();

	const crushed: number[] = [];
	let widest_evicted = 0;
	for (let width = FROM; width <= TO; width += STEP) {
		await page.setViewportSize({ width, height: 800 });
		await page.waitForTimeout(60);
		const probe = await _probeCommandEntry(page);
		if (probe.evicted) widest_evicted = Math.max(widest_evicted, width);
		else if (!probe.hittable) crushed.push(width);
	}

	expect(crushed, 'widths where the command entry is laid out but not clickable').toEqual([]);
	// A guard that only ever sees an evicted entry would pass vacuously, so
	// assert the eviction band is the narrow low-width one it is supposed to
	// be rather than the whole sweep.
	expect(widest_evicted, 'the command entry is evicted far above its breakpoint').toBeLessThan(900);
});

test('a pointer can travel from the SOURCE button into the menu without it closing', async ({
	page
}) => {
	await _openPerformance(page);
	await page.setViewportSize({ width: 1864, height: 947 });

	const toggleBox = await page.locator('.src-toggle').boundingBox();
	if (toggleBox === null) throw new Error('the SOURCE toggle has no layout box');
	const fromX = toggleBox.x + toggleBox.width / 2;
	const fromY = toggleBox.y + toggleBox.height / 2;
	await page.mouse.move(fromX, fromY);
	await expect(page.locator('.src-menu')).toBeVisible();

	const ownBox = await page.locator('.src-seg button', { hasText: 'OWN' }).boundingBox();
	if (ownBox === null) throw new Error('the OWN button has no layout box');
	const toX = ownBox.x + ownBox.width / 2;
	const toY = ownBox.y + ownBox.height / 2;
	// Walk the gap between the two boxes in small steps. Any uncovered screen
	// in it fires the wrapper's pointerleave with a relatedTarget that is
	// neither the wrapper nor the menu, which is what used to close the menu
	// mid-travel.
	const STEPS = 16;
	for (let step = 1; step <= STEPS; step++) {
		await page.mouse.move(fromX + ((toX - fromX) * step) / STEPS, fromY + ((toY - fromY) * step) / STEPS);
	}

	await expect(page.locator('.src-menu')).toBeVisible();
	const landed = await page.evaluate(() => {
		const own = [...document.querySelectorAll('.src-seg button')].find(
			(b) => b.textContent?.trim() === 'OWN'
		);
		if (own === undefined) return false;
		const rect = own.getBoundingClientRect();
		const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
		return hit === own || own.contains(hit);
	});
	expect(landed, 'the pointer could not reach OWN with the menu still open').toBe(true);
});
