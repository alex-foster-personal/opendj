/**
 * MIXUX-13 (orders board W38): Shift+click builds an ordered selection of
 * dials and a drag on a selected dial moves the selection. MIXGRP-01/02
 * (preview port, Fri 9 Oct 2026) replaced the old two-axis split: a drag on
 * any selected control moves the whole group, led by the dragged control, and
 * Opt held flips the other channel. The two-axis feel lives on the group pad
 * (MIXGRP-05).
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
 * - [if] dials on two decks are selected and an up drag on the second does not raise both by the same amount [then ⛔️]
 * - [if] the same drag with Opt held does not raise the dragged deck and lower the other [then ⛔️]
 * - [if] the selection is gone once the drag ends [then ⛔️]
 * - [if] Esc leaves either dial showing as selected [then ⛔️]
 * - [if] a shift-click on a channel LEVEL fader selects that deck's strip, or a drag on one
 *   of two shift-selected faders does not move the other by the same amount [then ⛔️]
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

test('two decks selected: a drag on either moves both, led by the dragged one; Opt flips the other deck; Esc clears', async ({ page }) => {
	await _open(page);
	const first = knob(page, '1:low');
	const other = knob(page, '2:low');
	await _shiftClick(page, first);
	await _shiftClick(page, other);

	// MIXGRP-01: the drag on the second-selected dial leads, and the first follows it.
	const firstBefore = await _value(first);
	const otherBefore = await _value(other);
	await _drag(page, other, 0, -24);
	await expect.poll(() => _value(other), { message: 'the dragged dial did not rise' })
		.toBeGreaterThan(otherBefore + 0.05);
	await expect.poll(() => _value(first), { message: 'the other selected deck did not follow the lead up' })
		.toBeGreaterThan(firstBefore + 0.05);
	expect(Math.abs((await _value(other)) - otherBefore - ((await _value(first)) - firstBefore))).toBeLessThan(0.01);

	// MIXGRP-02: Opt held flips the other channel, so the same drag splits them.
	const firstMid = await _value(first);
	const otherMid = await _value(other);
	await page.keyboard.down('Alt');
	await _drag(page, other, 0, -24);
	await page.keyboard.up('Alt');
	await expect.poll(() => _value(other), { message: 'with Opt the dragged deck did not rise' })
		.toBeGreaterThan(otherMid + 0.05);
	await expect.poll(() => _value(first), { message: 'with Opt the other deck did not go the opposite way' })
		.toBeLessThan(firstMid - 0.05);

	await expect(first).toHaveClass(/knob-selected/);
	await expect(other).toHaveClass(/knob-selected/);
	await page.keyboard.press('Escape');
	await expect(first).not.toHaveClass(/knob-selected/);
	await expect(other).not.toHaveClass(/knob-selected/);
});

test('shift-select two LEVEL faders: no deck select, and a drag on one moves both', async ({ page }) => {
	await _open(page);
	const first = page.getByTestId('channel-1-fader');
	const other = page.getByTestId('channel-2-fader');
	await expect(first).toHaveAttribute('data-knob-id', '1:fader');
	const strip = (deck: number): Locator => page.locator(`.strip[data-mixer-channel="${deck}"]`);
	const SELECTED = /(^|\s)selected(\s|$)/;
	// Boot selects deck 1 only (mixer-selection.svelte.ts); a leaked shift-click
	// on deck 2's fader would add deck 2 to that strip selection.
	const assertDeckSelectUntouched = async (): Promise<void> => {
		await expect(strip(1)).toHaveClass(SELECTED);
		await expect(strip(2), 'a shift-click on a fader also took deck select').not.toHaveClass(SELECTED);
	};
	await assertDeckSelectUntouched();
	await page.keyboard.down('Shift');
	await first.click();
	await other.click();
	await page.keyboard.up('Shift');
	await expect(first).toHaveClass(/fader-selected/);
	await expect(other).toHaveClass(/fader-selected/);
	// MIXUX-08 deck select belongs to a plain click on the strip; a shift-click
	// on its fader is a knob-selection gesture and must not also select the deck.
	await assertDeckSelectUntouched();

	const firstBefore = await _value(first);
	const otherBefore = await _value(other);
	// Down, so a fader parked at full travel still has room to move.
	await _drag(page, other, 0, 40);
	await expect.poll(() => _value(other), { message: 'the dragged fader did not move' })
		.toBeLessThan(otherBefore - 0.02);
	await expect.poll(() => _value(first), { message: 'the other selected fader did not follow the drag' })
		.toBeLessThan(firstBefore - 0.02);
	expect(Math.abs((await _value(other)) - otherBefore - ((await _value(first)) - firstBefore))).toBeLessThan(0.01);
	// The drag is a plain press, so its closing click may select the deck as any
	// plain strip click does (MIXUX-08); the fader selection must outlive it.
	await expect(first).toHaveClass(/fader-selected/);
	await expect(other).toHaveClass(/fader-selected/);
});
