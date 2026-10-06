/**
 * MIXUX-13 (orders board W38): Shift+click builds an ordered selection of
 * dials, a drag on a selected dial moves the selection, and with dials on two
 * decks the drag is two-axis: vertical drives the first-selected deck,
 * horizontal the other (right = up).
 *
 * the maintainer, Tue 6 Oct 2026: "we had a feature where we could select multiple knobs
 * with shift held. on any click&drag then or scroll etc (as per usual knob
 * adjustments) it then adjusts both / all knobs up and down together [...] up
 * and right is both / all knobs up, up and left is one up, one down".
 *
 * The unit tests pin the maths. This drives the rendered knobs in a real
 * browser through Knob.svelte's pointer path and the store round trip back
 * into aria-valuenow, which is what the DJ sees.
 *
 * Acceptance tests, "[if] <scenario> [then ⛔️]":
 * - [if] two EQ dials are shift-selected and one is dragged up, and either does not rise [then ⛔️]
 * - [if] dials on two decks are selected and an up+left drag does not raise the first and lower the second [then ⛔️]
 * - [if] the selection is gone once the drag ends [then ⛔️]
 * - [if] Esc leaves either dial showing as selected [then ⛔️]
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

const knob = (page: Page, id: string): Locator => page.locator(`[data-knob-id="${id}"]`);

async function _value(dial: Locator): Promise<number> {
	const raw = await dial.getAttribute('aria-valuenow');
	const value = raw === null ? Number.NaN : Number.parseFloat(raw);
	if (!Number.isFinite(value)) throw new Error(`knob aria-valuenow is not a number: ${raw}`);
	return value;
}

async function _open(page: Page): Promise<void> {
	// A cold vite compile of /performance can outlast the 30 s default.
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	await expect(knob(page, '1:low')).toBeVisible({ timeout: 60_000 });
	await waitForPerformanceIpc(page);
}

async function _shiftClick(page: Page, dial: Locator): Promise<void> {
	await page.keyboard.down('Shift');
	await dial.click();
	await page.keyboard.up('Shift');
	await expect(dial).toHaveClass(/knob-selected/);
}

/** Press on the dial centre, move by (dx, dy) screen px in steps, release. */
async function _drag(page: Page, dial: Locator, dx: number, dy: number): Promise<void> {
	const box = await dial.boundingBox();
	if (box === null) throw new Error('knob has no bounding box');
	const x = box.x + box.width / 2;
	const y = box.y + box.height / 2;
	await page.mouse.move(x, y);
	await page.mouse.down();
	await page.mouse.move(x + dx, y + dy, { steps: 6 });
	await page.mouse.up();
}

test('shift-select two EQ dials, drag one up: both rise', async ({ page }) => {
	await _open(page);
	const low = knob(page, '1:low');
	const high = knob(page, '1:high');
	const lowBefore = await _value(low);
	const highBefore = await _value(high);
	await _shiftClick(page, low);
	await _shiftClick(page, high);

	await _drag(page, low, 0, -24);

	await expect.poll(() => _value(low)).toBeGreaterThan(lowBefore + 0.1);
	await expect.poll(() => _value(high), { message: 'the second selected dial did not move with the dragged one' })
		.toBeGreaterThan(highBefore + 0.1);
	expect(Math.abs((await _value(low)) - lowBefore - ((await _value(high)) - highBefore))).toBeLessThan(0.01);
	// The selection outlives the drag (a re-render once wiped it on the first frame).
	await expect(low).toHaveClass(/knob-selected/);
	await expect(high).toHaveClass(/knob-selected/);
});

test('two decks selected: up+left raises the first-selected deck and lowers the other; Esc clears', async ({ page }) => {
	await _open(page);
	const first = knob(page, '1:low');
	const other = knob(page, '2:low');
	const firstBefore = await _value(first);
	const otherBefore = await _value(other);
	await _shiftClick(page, first);
	await _shiftClick(page, other);

	await _drag(page, other, -18, -18);

	await expect.poll(() => _value(first), { message: 'up did not raise the first-selected deck' })
		.toBeGreaterThan(firstBefore + 0.08);
	await expect.poll(() => _value(other), { message: 'left did not lower the other deck' })
		.toBeLessThan(otherBefore - 0.08);

	await expect(first).toHaveClass(/knob-selected/);
	await expect(other).toHaveClass(/knob-selected/);
	await page.keyboard.press('Escape');
	await expect(first).not.toHaveClass(/knob-selected/);
	await expect(other).not.toHaveClass(/knob-selected/);
});
