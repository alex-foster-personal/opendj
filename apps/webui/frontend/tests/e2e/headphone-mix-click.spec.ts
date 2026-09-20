/**
 * Issue #3533 (CUEOUT-01): single-clicking the headphone MIX knob steps it by
 * exactly one third, a double-click resets it without a stray step, and a click
 * with a pixel of pointer jitter is still a click.
 *
 * The unit tests pin the step arithmetic and the guard in isolation. What they
 * cannot see is the production event path: Knob.svelte's pointerdown/up and
 * dblclick ordering, the real 500 ms single-click timer, and the store round
 * trip back into aria-valuenow. So this drives the rendered knob in a real
 * browser and reads the value the knob itself reports.
 *
 * Lives in the root suite for the same reason as deck-loader-placement.spec.ts:
 * /performance only mounts behind a real backend with a fixture library, which
 * the root config builds and e2e.yml runs on every PR.
 *
 * Acceptance tests, "[if] <scenario> [then ⛔️]":
 * - [if] a single click has moved MIX before the double-click window closes [then ⛔️]
 * - [if] a single click does not move MIX by exactly one third [then ⛔️]
 * - [if] a double-click leaves MIX anywhere but 0 once every timer has fired [then ⛔️]
 * - [if] a click with 2 px of pointer travel does not step MIX [then ⛔️]
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

const MIX_KNOB = '[data-testid="knob-hp:hp-mix"]';
const THIRD = 1 / 3;
/** KNOB_SINGLE_CLICK_DELAY_MS (deferred-click.ts) plus slack for a loaded runner. */
const PAST_SINGLE_CLICK_MS = 900;
/** Well inside the 500 ms window: the step must not have fired yet. */
const INSIDE_SINGLE_CLICK_MS = 250;

async function _openMixKnob(page: Page): Promise<Locator> {
	await page.setViewportSize({ width: 1280, height: 1000 });
	await page.goto('/performance');
	const knob = page.locator(MIX_KNOB);
	await expect(knob).toBeVisible({ timeout: 60_000 });
	return knob;
}

async function _mixValue(knob: Locator): Promise<number> {
	const raw = await knob.getAttribute('aria-valuenow');
	if (raw === null) throw new Error('MIX knob has no aria-valuenow');
	const value = Number.parseFloat(raw);
	if (!Number.isFinite(value)) throw new Error(`MIX knob aria-valuenow is not a number: ${raw}`);
	return value;
}

/** A press and release at the knob centre, moving `jitterPx` in between. */
async function _clickWithJitter(page: Page, knob: Locator, jitterPx: number): Promise<void> {
	const box = await knob.boundingBox();
	if (box === null) throw new Error('MIX knob has no bounding box');
	const x = box.x + box.width / 2;
	const y = box.y + box.height / 2;
	await page.mouse.move(x, y);
	await page.mouse.down();
	if (jitterPx > 0) await page.mouse.move(x + jitterPx, y, { steps: 2 });
	await page.mouse.up();
}

test('a single click steps MIX by exactly one third, after the double-click window', async ({ page }) => {
	const knob = await _openMixKnob(page);
	const before = await _mixValue(knob);
	await knob.click();
	await page.waitForTimeout(INSIDE_SINGLE_CLICK_MS);
	expect(await _mixValue(knob), 'the step fired before a double-click could cancel it').toBeCloseTo(before, 6);
	await expect.poll(async () => Math.abs((await _mixValue(knob)) - before), { timeout: 5_000 }).toBeCloseTo(THIRD, 6);
});

test('a double-click resets MIX to 0 and no delayed single-click step follows', async ({ page }) => {
	const knob = await _openMixKnob(page);
	// Move off 0 first, so a stray one-third step after the reset is visible.
	await knob.click();
	await expect.poll(async () => _mixValue(knob), { timeout: 5_000 }).not.toBeCloseTo(0, 6);
	await knob.dblclick();
	await page.waitForTimeout(PAST_SINGLE_CLICK_MS);
	expect(await _mixValue(knob), 'a double-click let a delayed single-click step through').toBeCloseTo(0, 6);
});

test('a click with 2 px of pointer jitter still steps MIX', async ({ page }) => {
	const knob = await _openMixKnob(page);
	const before = await _mixValue(knob);
	await _clickWithJitter(page, knob, 2);
	await expect.poll(async () => Math.abs((await _mixValue(knob)) - before), { timeout: 5_000 }).toBeCloseTo(THIRD, 6);
});
