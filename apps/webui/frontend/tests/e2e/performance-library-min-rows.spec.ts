import { expect, test } from '@playwright/test';

// LIBUX-01 e2e smoke - "Library panel should be slightly greedier - minimum
// 5 rows of songs." A real browser is the only thing that can prove the CSS
// cascade (perf-root's grid reservation + TrackTable's flex-shrink floor)
// actually resolves the way the arithmetic in library-min-5-rows.test.mjs
// predicts; that unit test only checks the source, not the render.
//
// No library data is required: TrackTable always renders its <table><thead>
// regardless of row count (see TrackTable.svelte - the empty-state message
// is an overlay sibling, not a table replacement), so this is a pure
// geometry check.
//
// Two floors compete for the same vertical space (see +page.svelte's
// perf-root comment, PR #1007 discussions r3921198996 and r3921321752):
// the deck area's documented 497px two-deck-column content-tight floor
// (protected via `minmax(497px, ...)`, since `.rb-deck` uses
// overflow: hidden and a shorter box genuinely clips controls), and the
// library's 255px 5-row floor. Their sum plus topbar/wave (200px) is 952px,
// taller than the repo's standard 1280x800 viewport, so only ONE floor can
// be fully satisfied below that height. Decks win the conflict (protecting
// already-shipped controls), so this file tests each floor at the viewport
// where it is actually supposed to hold, instead of asserting both at once
// somewhere neither can be true.

const TALL_VIEWPORT = { width: 1280, height: 1000 };
// thead (20px, fixed) + 5 * compact row height (22px, the default density) +
// a 17px classic-scrollbar-gutter allowance - mirrors TrackTable.svelte's
// `.tt-root { min-height: calc(20px + 5 * var(--tt-row-h) + 17px) }`
// (PR #1007 discussion r3921198996: table-wrap's default column widths
// exceed this viewport's width, so a horizontal scrollbar is real; this
// sandbox's Chromium happens to render overlay scrollbars, which cost 0
// layout height, so this assertion cannot itself distinguish "budgeted the
// 17px and it went unused" from "the budget is wrong" - see NOT-verified in
// the PR body). 1000px is tall enough that the deck-area floor (497px) is
// not in the way (952px needed for both floors at once, 1000 > 952).
const MIN_TABLE_WRAP_HEIGHT = 20 + 5 * 22 + 17;

test('performance: library table-wrap keeps a 5-row floor once the window is tall enough for both floors', async ({
	page
}) => {
	await page.setViewportSize(TALL_VIEWPORT);
	await page.goto('/performance');

	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible();

	const box = await tableWrap.boundingBox();
	expect(box).not.toBeNull();
	// -1px tolerance for subpixel layout rounding across browser engines.
	expect(box!.height).toBeGreaterThanOrEqual(MIN_TABLE_WRAP_HEIGHT - 1);
});

// Content-tight per-deck floor from +page.svelte's own perf-root comment
// ("one deck needs 248px"): header 75 + strip 28 + main-row 113 + stems 16 +
// padding/gaps 16. PR #1007 discussion r3921321752 found that LIBUX-01's
// library reservation pushed decks to ~172px at the repo's standard
// 1280x800 viewport, well below this floor, clipping the bottom-most
// control row (StemRow's `.stems`) via `.rb-deck`'s overflow: hidden.
const STANDARD_VIEWPORT = { width: 1280, height: 800 };
const MIN_DECK_HEIGHT = 248;

test('performance: deck keeps its content-tight floor (no control clipping) at the standard 1280x800 viewport', async ({
	page
}) => {
	await page.setViewportSize(STANDARD_VIEWPORT);
	await page.goto('/performance');

	const deck = page.locator('.rb-deck').first();
	await expect(deck).toBeVisible();

	const deckBox = await deck.boundingBox();
	expect(deckBox).not.toBeNull();
	expect(deckBox!.height).toBeGreaterThanOrEqual(MIN_DECK_HEIGHT - 1);

	// The bottom-most control row must actually render inside the deck's
	// visible box, not just exist in the DOM behind overflow: hidden.
	const stems = deck.locator('.stems');
	await expect(stems).toBeVisible();
	const stemsBox = await stems.boundingBox();
	expect(stemsBox).not.toBeNull();
	expect(stemsBox!.y + stemsBox!.height).toBeLessThanOrEqual(deckBox!.y + deckBox!.height + 1);
});
