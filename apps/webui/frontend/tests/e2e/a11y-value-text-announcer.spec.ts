/**
 * A11Y-11269: aria-valuetext on sliders and the single polite announcer.
 *
 * Acceptance:
 * - [if] the MID knob is dragged [then] aria-valuetext tracks eqDbFromKnob(aria-valuenow), [else stop]
 * - [if] pitch valuetext and jog label disagree on whole percent [then] fail, [else stop]
 * - [if] deck 3 is clicked twice [then] the announcer count is unchanged after 700 ms, [else stop]
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

const EQ_MIN_DB = -26;
const EQ_MAX_DB = 6;

function eqDbFromKnob(value: number): number {
	if (value <= 0.5) return EQ_MIN_DB * (1 - value * 2);
	return EQ_MAX_DB * (value * 2 - 1);
}

function formatSignedDb(db: number): string {
	const rounded = Math.round(db * 10) / 10;
	if (rounded === 0 || Object.is(rounded, -0)) return '0 dB';
	const body = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
	return rounded > 0 ? `+${body} dB` : `${body} dB`;
}

async function _open(page: Page): Promise<void> {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	await expect(page.locator('[data-knob-id="1:mid"]')).toBeVisible({ timeout: 60_000 });
	await waitForPerformanceIpc(page);
}

async function _dragKnob(page: Page, dial: Locator, dy: number): Promise<void> {
	const box = await dial.boundingBox();
	if (box === null) throw new Error('knob has no bounding box');
	const x = box.x + box.width / 2;
	const y = box.y + box.height / 2;
	await page.mouse.move(x, y);
	await page.mouse.down();
	await page.mouse.move(x, y + dy, { steps: 6 });
	await page.mouse.up();
}

test('A11Y-11269 MID knob aria-valuetext tracks eq law while dragging', async ({ page }) => {
	await _open(page);
	const mid = page.locator('[data-knob-id="1:mid"]');
	const before = await mid.getAttribute('aria-valuetext');
	await _dragKnob(page, mid, -24);
	await expect
		.poll(async () => mid.getAttribute('aria-valuetext'), {
			message: 'MID knob valuetext must change after a drag'
		})
		.not.toEqual(before);
	const nowRaw = await mid.getAttribute('aria-valuenow');
	const value = nowRaw === null ? Number.NaN : Number.parseFloat(nowRaw);
	const expected = formatSignedDb(eqDbFromKnob(value));
	await expect(mid).toHaveAttribute('aria-valuetext', expected);
});

test('A11Y-11269 pitch fader valuetext agrees with jog whole-percent label', async ({ page }) => {
	await _open(page);
	const pitchFader = page.getByTestId('pitch-fader-deck-1');
	const valuetext = await pitchFader.getAttribute('aria-valuetext');
	expect(valuetext, 'pitch fader must expose aria-valuetext').not.toBeNull();
	if (valuetext === null) return;
	const jogPitch = page.locator('section.rb-deck[data-deck="1"] text.pitch').first();
	await expect(jogPitch).toBeVisible();
	const label = (await jogPitch.textContent())?.trim() ?? '';
	const valNum = Number.parseFloat(valuetext.replace('%', '').replace('+', ''));
	if (label === '±0%') {
		expect(Math.abs(valNum)).toBeLessThan(0.5);
	} else {
		const labelNum = Number.parseInt(label.replace('%', '').replace('+', ''), 10);
		expect(Math.round(valNum)).toEqual(labelNum);
		expect(Math.sign(valNum)).toEqual(label.startsWith('-') ? -1 : label.startsWith('+') ? 1 : 0);
	}
});

test('A11Y-11269 single announcer reports deck selection with dedupe', async ({ page }) => {
	await _open(page);
	const announcer = page.locator('[data-testid="a11y-announcer"]');
	await expect(page.locator('[aria-live="polite"][data-testid="a11y-announcer"]')).toHaveCount(1);
	const ch2 = page.locator('[data-mixer-channel="2"]').first();
	const ch3 = page.locator('[data-mixer-channel="3"]').first();
	await ch2.click({ position: { x: 8, y: 8 } });
	await expect(announcer).toHaveText('Deck 2 selected');
	await ch3.click({ position: { x: 8, y: 8 } });
	await expect(announcer).toHaveText('Deck 3 selected');
	const countAfter = await announcer.getAttribute('data-announce-count');
	await ch3.click({ position: { x: 8, y: 8 } });
	await page.waitForTimeout(700);
	await expect(announcer).toHaveAttribute('data-announce-count', countAfter ?? '');
});

test('A11Y-11269 hot cue C set and cleared announce without toasts', async ({ page }) => {
	const announcer = page.locator('[data-testid="a11y-announcer"]');
	const toastCount = async () => page.locator('[data-toast-id]').count();

	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const firstRow = page.locator('.table-wrap table tbody tr[data-stable-id]').first();
	if (!(await firstRow.isVisible({ timeout: 15_000 }).catch(() => false))) {
		test.skip(true, 'no loadable fixture tracks on this dev server');
		return;
	}
	const toastsBefore = await toastCount();
	await firstRow.locator('td.c-title').dblclick();
	const confirmYes = page.locator('.load-confirm[role="dialog"] .load-confirm-yes');
	if (await confirmYes.isVisible()) await confirmYes.click();
	await page.waitForFunction(
		() => window.musicDjToolsPerformance?.query().decks[1].stable_id !== null
	);

	const deckPanel = page.locator('section.rb-deck[data-deck="1"]');
	const padC = deckPanel.getByTestId('hot-cue-1-C');
	await padC.click();
	const nameInput = deckPanel.getByTestId('hot-cue-name-1-C');
	await expect(nameInput).toBeVisible();
	await nameInput.fill('a11y cue');
	await nameInput.press('Enter');
	await expect(announcer).toHaveText('Hot cue C set');
	expect(await toastCount()).toEqual(toastsBefore);

	const clearBtn = deckPanel.getByRole('button', { name: /clear hot cue C deck 1/i });
	await clearBtn.click();
	await expect(announcer).toHaveText('Hot cue C cleared');
	expect(await toastCount()).toEqual(toastsBefore);
});
