/**
 * Issue #1588: the 100ms deck-btns hide corridor must actually delay
 * pointer-events, not merely exist as CSS text.
 *
 * `.deck-btns` hangs over the previous row (`bottom: 100%`) with
 * `pointer-events: none` on the box itself. Travelling from `.c-title` up
 * to a 16px button therefore crosses a gap whose hit-test is the row above.
 * Without a hide delay the buttons go `pointer-events: none` the instant
 * `.c-title:hover` drops, so `.deck-btns:hover` can never latch.
 *
 * The old CSS rule (`transition: pointer-events 0s 100ms`) is discrete and
 * only runs with `transition-behavior: allow-discrete`, which the macOS 11
 * WKWebView floor does not have - the same gap PR #1570 found for the
 * select-driven reveal. This spec therefore asserts the DOWNSTREAM effect
 * of the JS timer (`.corridor-grace-active`, CORRIDOR_GRACE_MS = 100) in a
 * real browser: computed pointer-events stay `auto` across that gap, then
 * become `none` after the grace window. A source-regex of the CSS cannot
 * fail when the transition never executes.
 *
 * WHY THIS FILE LIVES IN THE ROOT SUITE: same as
 * `deck-loader-placement.spec.ts` - it needs the throwaway fixture library
 * the root config builds, and it is the per-PR chromium gate. Not adding a
 * WebKit project here (#1558: that engine gap is a separate decision).
 *
 * Layout constraints copied from the placement spec: 1280x1000 (at 720px
 * the deck area eats the library hit-test) and row index 1 (needs a row
 * above, which is the actual gap).
 *
 * Acceptance:
 * - [if] the pointer leaves the selected row's title into the hanging-box
 *   gap [then ⛔️] the first deck button must still be `pointer-events: auto`
 *   on that same turn - an instant hide is the inert-CSS regression.
 * - [if] the pointer then rests in that gap for ~180ms [then ⛔️] the same
 *   button must be `pointer-events: none` - grace that never clears is not
 *   a 100ms corridor.
 * - [if] the pointer hops title → gap → a deck button in one-step moves
 *   [then ⛔️] the button centre must resolve to that button, proving the
 *   corridor is actually travellable.
 */
import { expect, test, type Page } from '@playwright/test';

const TRACK_ROW = '[data-testid="track-row"]';
/**
 * 1000px tall, NOT the suite's default 720: below ~969px the deck area
 * wins the vertical floor and its overflow takes the library hit-test
 * with it (see deck-loader-placement.spec.ts).
 */
const VIEWPORT = { width: 1280, height: 1000 };
const ROW_INDEX = 1;
const PAST_DBLCLICK_GUARD_MS = 700;
/** 100ms corridor + scheduling margin. Do not wait seconds. */
const PAST_CORRIDOR_MS = 180;

async function _openAllTracks(page: Page): Promise<void> {
	await page.setViewportSize(VIEWPORT);
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-table"]')).toBeVisible({ timeout: 60_000 });
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator(TRACK_ROW).nth(ROW_INDEX)).toBeVisible({ timeout: 60_000 });
}

async function _selectAndHover(page: Page, index: number) {
	const row = page.locator(TRACK_ROW).nth(index);
	const title = row.locator('td.c-title');
	await title.click();
	await expect(row, 'the row did not select, so the quick-load box stays hidden').toHaveClass(
		/rb-row-selected/
	);
	await title.hover();
	return { row, title };
}

type CorridorGeometry = {
	titleX: number;
	titleY: number;
	gapX: number;
	gapY: number;
	deck2X: number;
	deck2Y: number;
	clusterLeft: number;
	boxLeft: number;
	boxTop: number;
	boxBottom: number;
	titleTop: number;
	gapHit: string;
	gapIsButton: boolean;
	gapIsThisRowTrigger: boolean;
	pointerEvents: string;
};

async function _corridorGeometry(page: Page): Promise<CorridorGeometry> {
	return page.evaluate(() => {
		const selected = document.querySelector<HTMLElement>(
			'[data-testid="track-row"].rb-row-selected'
		);
		if (selected === null) throw new Error('no selected row');
		const title = selected.querySelector<HTMLElement>('td.c-title');
		const art = selected.querySelector<HTMLElement>('td.c-art');
		const box = selected.querySelector<HTMLElement>('.deck-btns');
		if (title === null || box === null) throw new Error('missing title or box');
		const buttons = [...box.querySelectorAll<HTMLElement>('button')];
		if (buttons.length === 0) throw new Error('no buttons');
		const deck2 = box.querySelector<HTMLElement>('button[title="Load onto deck 2"]');
		if (deck2 === null) throw new Error('no deck 2 button');
		const titleRect = title.getBoundingClientRect();
		const boxRect = box.getBoundingClientRect();
		const clusterLeft = Math.min(...buttons.map((b) => b.getBoundingClientRect().left));
		const gapX = (boxRect.left + clusterLeft) / 2;
		const gapY = boxRect.top + boxRect.height / 2;
		const gapHitEl = document.elementFromPoint(gapX, gapY);
		const describe = (el: Element | null) =>
			el === null ? 'null' : `${el.tagName}.${el.className}`;
		const deck2Rect = deck2.getBoundingClientRect();
		const first = buttons[0];
		if (first === undefined) throw new Error('no first button');
		return {
			titleX: titleRect.left + titleRect.width / 2,
			titleY: titleRect.top + titleRect.height / 2,
			gapX,
			gapY,
			deck2X: deck2Rect.left + deck2Rect.width / 2,
			deck2Y: deck2Rect.top + deck2Rect.height / 2,
			clusterLeft,
			boxLeft: boxRect.left,
			boxTop: boxRect.top,
			boxBottom: boxRect.bottom,
			titleTop: titleRect.top,
			gapHit: describe(gapHitEl),
			gapIsButton: gapHitEl !== null && gapHitEl.closest('button') !== null,
			gapIsThisRowTrigger:
				gapHitEl !== null &&
				(title.contains(gapHitEl) || (art !== null && art.contains(gapHitEl))),
			pointerEvents: getComputedStyle(first).pointerEvents
		};
	});
}

async function _firstButtonPointerEvents(page: Page): Promise<string> {
	return page.evaluate(() => {
		const selected = document.querySelector<HTMLElement>(
			'[data-testid="track-row"].rb-row-selected'
		);
		if (selected === null) throw new Error('no selected row');
		const button = selected.querySelector<HTMLElement>('.deck-btns button');
		if (button === null) throw new Error('no deck button');
		return getComputedStyle(button).pointerEvents;
	});
}

test('leaving the title into the hanging-box gap delays pointer-events by ~100ms, then clears', async ({
	page
}) => {
	test.setTimeout(120_000);
	await _openAllTracks(page);
	const { row } = await _selectAndHover(page, ROW_INDEX);
	await expect(row.locator('.deck-btns')).toBeVisible();
	await page.waitForTimeout(PAST_DBLCLICK_GUARD_MS);
	await expect(row.locator('.deck-btns button').first()).toHaveCSS('pointer-events', 'auto');

	const geo = await _corridorGeometry(page);
	expect(
		geo.boxBottom,
		`the gap must sit in the hanging box above the title: ${JSON.stringify(geo)}`
	).toBeLessThanOrEqual(geo.titleTop + 1);
	expect(geo.gapX, `gap x must be left of the button cluster: ${JSON.stringify(geo)}`).toBeLessThan(
		geo.clusterLeft
	);
	expect(
		geo.gapIsButton,
		`the gap point must not be a button (that is the cluster, not the corridor): ${geo.gapHit}`
	).toBe(false);
	expect(
		geo.gapIsThisRowTrigger,
		`the gap point must not still be this row's .c-art/.c-title (leave would not fire): ${geo.gapHit}`
	).toBe(false);

	await page.mouse.move(geo.gapX, geo.gapY, { steps: 1 });
	const duringGrace = await _firstButtonPointerEvents(page);
	expect(
		duringGrace,
		'computed pointer-events must stay auto immediately after leaving the title into the gap - an instant hide means the corridor never ran'
	).toBe('auto');

	await page.waitForTimeout(PAST_CORRIDOR_MS);
	const afterGrace = await _firstButtonPointerEvents(page);
	expect(
		afterGrace,
		'computed pointer-events must be none ~180ms later - grace that never clears is not a 100ms corridor'
	).toBe('none');
});

test('travelling title → gap → a deck button lands on the button', async ({ page }) => {
	test.setTimeout(120_000);
	await _openAllTracks(page);
	const { row } = await _selectAndHover(page, ROW_INDEX);
	await expect(row.locator('.deck-btns')).toBeVisible();
	await page.waitForTimeout(PAST_DBLCLICK_GUARD_MS);
	await expect(row.locator('.deck-btns button').first()).toHaveCSS('pointer-events', 'auto');

	const geo = await _corridorGeometry(page);
	expect(geo.gapIsButton, `gap point is a button, not the corridor: ${geo.gapHit}`).toBe(false);

	await page.mouse.move(geo.titleX, geo.titleY, { steps: 1 });
	await page.mouse.move(geo.gapX, geo.gapY, { steps: 1 });
	await page.mouse.move(geo.deck2X, geo.deck2Y, { steps: 1 });

	// Identify deck 2 by its label, not its `title`: while the pointer rests on
	// a control the single hover tooltip (#5397) parks `title` on `data-tip` so
	// WKWebView does not draw a second, native tooltip.
	const landed = await page.evaluate(({ x, y }) => {
		const selected = document.querySelector<HTMLElement>(
			'[data-testid="track-row"].rb-row-selected'
		);
		const deck2 = [
			...(selected?.querySelectorAll<HTMLElement>('.deck-btns button.deck-target') ?? [])
		].find((b) => b.textContent?.trim() === '2');
		const hit = document.elementFromPoint(x, y);
		if (hit === null || deck2 === undefined) return { ok: false, tip: null, stack: 'null' };
		const button = hit.closest('button');
		const stack = document
			.elementsFromPoint(x, y)
			.slice(0, 5)
			.map((el) => `${el.tagName}.${el.className}`)
			.join(' > ');
		return {
			ok: button === deck2,
			tip: deck2.getAttribute('title') ?? deck2.getAttribute('data-tip'),
			stack
		};
	}, { x: geo.deck2X, y: geo.deck2Y });

	expect(
		landed.ok,
		`after the gap hop, the deck-2 button centre must resolve to that button; hit stack: ${landed.stack}`
	).toBe(true);
	expect(landed.tip, 'the landed button must be the deck-2 loader').toBe('Load onto deck 2');
});
