/**
 * requirement: COLORFX-15 (also drives COLORFX-10 and COLORFX-8341)
 * Issue #9799, closes #579: a real browser drives the deck 1 color FX menu.
 * MIXUX-02, the maintainer verbatim: "Filter dial should be white bg instead of black for clarity."
 *
 * [if] deck 1 is on FILTER and its color dial face is not light [then ⛔️]
 * [if] a tap on the color dial caption does not open the menu [then ⛔️]
 * [if] picking SPACE leaves the caption or ui-mirror color_fx["1"].effect on anything but SPACE [then ⛔️]
 * [if] SPACE keeps the white face [then ⛔️]
 * [if] a drag on the color dial (or another knob) while the menu is open does not turn it in that same gesture [then ⛔️]
 * [if] a Cmd-click (Ctrl-click off a Mac) on the caption does not cycle to the next effect, or opens the menu [then ⛔️]
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

const knob = (page: Page, id: string): Locator => page.locator(`[data-knob-id="${id}"]`);
const colorKnob = (page: Page): Locator => knob(page, '1:filter');
const caption = (page: Page): Locator => colorKnob(page).locator('.label-text');
const menu = (page: Page): Locator => page.getByTestId('color-fx-menu-1');

async function dispatch(page: Page, command: Record<string, unknown>): Promise<void> {
	await page.evaluate((command) => {
		if (window.musicDjToolsPerformance === undefined) throw new Error('performance IPC unavailable');
		return window.musicDjToolsPerformance.dispatch(command as never);
	}, command);
}

async function open(page: Page): Promise<void> {
	// A cold vite compile of /performance can outlast the 30 s default.
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	await expect(colorKnob(page)).toBeVisible({ timeout: 60_000 });
	await waitForPerformanceIpc(page);
	await dispatch(page, { type: 'color_fx', deck: 1, effect: 'filter' });
	await expect(caption(page)).toHaveText('FILTER');
}

async function mirrorEffect(page: Page, deck: number): Promise<string | null> {
	return page.evaluate(async (deck) => {
		const response = await fetch('/api/v1/state/ui-mirror');
		if (!response.ok) return null;
		const doc = (await response.json()) as { color_fx?: Record<string, { effect?: string }> };
		return doc.color_fx?.[String(deck)]?.effect ?? null;
	}, deck);
}

async function knobValue(dial: Locator): Promise<number> {
	const raw = await dial.getAttribute('aria-valuenow');
	const value = raw === null ? Number.NaN : Number.parseFloat(raw);
	if (!Number.isFinite(value)) throw new Error(`knob aria-valuenow is not a number: ${raw}`);
	return value;
}

/** Relative luminance (0..1) of the color dial's rendered face fill. */
async function faceLuminance(page: Page): Promise<number> {
	const fill = await colorKnob(page).locator('svg .cap').evaluate((el) => getComputedStyle(el).fill);
	// rgb(r, g, b) for a plain fill; color(srgb r g b) once color-mix (the tint) is in play.
	const rgb = fill.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
	const srgb = fill.match(/color\(srgb ([\d.]+) ([\d.]+) ([\d.]+)/);
	let channels: number[];
	if (rgb !== null) channels = rgb.slice(1, 4).map((c) => Number(c) / 255);
	else if (srgb !== null) channels = srgb.slice(1, 4).map(Number);
	else throw new Error(`face fill is neither rgb() nor color(srgb): ${fill}`);
	const [r, g, b] = channels.map((s) => (s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4));
	return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}

/** Press on the dial (the SVG, not the caption), move up in steps, release. */
async function dragUp(page: Page, dial: Locator): Promise<void> {
	const box = await dial.locator('svg').first().boundingBox();
	if (box === null) throw new Error('knob dial has no bounding box');
	const x = box.x + box.width / 2;
	const y = box.y + box.height / 2;
	await page.mouse.move(x, y);
	await page.mouse.down();
	await page.mouse.move(x, y - 40, { steps: 8 });
	await page.mouse.up();
}

test('a tap on the color dial caption opens the menu and picking SPACE sets caption and mirror', async ({ page }) => {
	await open(page);
	expect(await faceLuminance(page)).toBeGreaterThan(0.8);
	await expect(menu(page)).toHaveCount(0);
	await caption(page).click();
	await expect(menu(page)).toBeVisible();
	await expect(colorKnob(page)).toHaveAttribute('data-popup-open', 'true');
	await page.getByTestId('color-fx-option-1-space').click();
	await expect(menu(page)).toHaveCount(0);
	await expect(caption(page)).toHaveText('SPACE');
	await expect.poll(() => mirrorEffect(page, 1), { timeout: 15_000 }).toBe('space');
	expect(await faceLuminance(page)).toBeLessThan(0.3);
});

test('a drag on the color dial while the menu is open turns the knob in that same gesture', async ({ page }) => {
	await open(page);
	await caption(page).click();
	await expect(menu(page)).toBeVisible();
	const before = await knobValue(colorKnob(page));
	await dragUp(page, colorKnob(page));
	await expect.poll(() => knobValue(colorKnob(page))).toBeGreaterThan(before + 0.05);
	await expect(menu(page)).toHaveCount(0);

	// Control: COLORFX-10, "the click drag doesn't catch the first click as the dropdown hasn't closed yet".
	await caption(page).click();
	await expect(menu(page)).toBeVisible();
	const low = knob(page, '1:low');
	const lowBefore = await knobValue(low);
	await dragUp(page, low);
	await expect.poll(() => knobValue(low)).toBeGreaterThan(lowBefore + 0.05);
	await expect(menu(page)).toHaveCount(0);
});

test('a Cmd-click (Ctrl-click off a Mac) on the caption cycles to the next effect', async ({ page }) => {
	await open(page);
	const modifier = await page.evaluate(() => (/mac|iphone|ipad/i.test(navigator.platform) ? 'Meta' : 'Control'));
	await caption(page).click({ modifiers: [modifier] });
	await expect(caption(page)).toHaveText('SPACE');
	await expect.poll(() => mirrorEffect(page, 1), { timeout: 15_000 }).toBe('space');
	await expect(menu(page)).toHaveCount(0);
	await caption(page).click({ modifiers: [modifier] });
	await expect(caption(page)).toHaveText('ECHO');
	await expect.poll(() => mirrorEffect(page, 1), { timeout: 15_000 }).toBe('echo');
	await expect(menu(page)).toHaveCount(0);
});
