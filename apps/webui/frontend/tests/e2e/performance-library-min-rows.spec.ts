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
// perf-root comment, PR #1007 discussions r3921198996, r3921321752,
// r3921443899 and r3923591731): the deck area's documented 497px two-deck-
// column content-tight floor (protected via `minmax(497px, ...)`, since
// `.rb-deck` uses overflow: hidden and a shorter box genuinely clips
// controls), and the library's 272px 5-row floor. Their sum plus topbar/wave
// (200px) is 969px, taller than the repo's standard 1280x800 viewport, so
// only ONE floor can be fully satisfied below that height. Decks win the
// conflict (protecting already-shipped controls), so this file tests each
// floor at the viewport where it is actually supposed to hold, instead of
// asserting both at once somewhere neither can be true - and then tests, AT
// 1280x800, what the losing side does with the shortfall, which is where the
// real defect was.

// 1000px clears the >= 969px threshold at which BOTH floors fit (497 deck +
// 272 library + 200 topbar/wave), so the 5-row guarantee is actually
// claimable here. Below it the guarantee does not hold and the tests further
// down assert what happens instead, rather than pretending it does.
const TALL_VIEWPORT = { width: 1280, height: 1000 };
// thead (20px, fixed) + 5 * compact row height (22px, the default density) +
// a 17px classic-scrollbar-gutter allowance - mirrors TrackTable.svelte's
// `.tt-root { min-height: calc(20px + 5 * var(--tt-row-h) + 17px +
// var(--tt-truncation-h)) }` with no truncation banner showing
// (PR #1007 discussion r3921198996: table-wrap's default column widths
// exceed this viewport's width, so a horizontal scrollbar is real; this
// sandbox's Chromium happens to render overlay scrollbars, which cost 0
// layout height, so this assertion cannot itself distinguish "budgeted the
// 17px and it went unused" from "the budget is wrong" - see NOT-verified in
// the PR body). 1000px is tall enough that the deck-area floor (497px) is
// not in the way (969px needed for both floors at once, 1000 > 969).
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

// PR #1007 discussion r3921443899 (P2/BLOCKING): "at the configured 1280x800
// Playwright viewport ... only 103px for the browser, versus the 255px needed
// ... Because `.perf-root` hides overflow, TrackTable's larger `min-height`
// cannot make those rows visible."
//
// The arithmetic is confirmed and unfixable at that height: the deck floor
// (497px, itself a BLOCKING finding from r3921321752 and asserted above) plus
// the library floor (272px) plus topbar/wave (200px) is 969px, so at 800px
// one of the two must give and the decks win. What was a real, separate
// defect is what the losing side DID: `.tt-root`'s min-height is absolute, so
// the table kept its floor height and painted straight out of the panel -
// measured on main at 1280x800, `.tt-root` ran to y=869 in an 800px window,
// `.perf-root` reported scrollHeight 895 against clientHeight 800 (95px of UI
// outside the window), and a hit test at the centre of the browser's bottom
// bar landed inside `.table-wrap`, i.e. the track list was painted on top of
// the bottom bar.
//
// `.list-panel { overflow: hidden }` (BrowserPanel.svelte) confines the
// shortfall to "fewer rows visible". These two tests are the red-then-green
// evidence for that: both fail on main (scrollHeight 895 > 801; hit test in
// `.tt-root`) and pass with the clip in place.
const OVERFLOW_TOLERANCE_PX = 2;

test('performance: the library floor never pushes UI outside the window at the standard 1280x800 viewport', async ({
	page
}) => {
	await page.setViewportSize(STANDARD_VIEWPORT);
	await page.goto('/performance');
	await expect(page.locator('.table-wrap')).toBeVisible();

	const overflow = await page.evaluate(() => {
		const root = document.querySelector('.perf-root');
		if (root === null) {
			throw new Error('.perf-root not found');
		}
		return { scrollHeight: root.scrollHeight, clientHeight: root.clientHeight };
	});

	expect(overflow.scrollHeight).toBeLessThanOrEqual(
		overflow.clientHeight + OVERFLOW_TOLERANCE_PX
	);
});

test('performance: the track list never paints over the browser bottom bar at 1280x800', async ({
	page
}) => {
	await page.setViewportSize(STANDARD_VIEWPORT);
	await page.goto('/performance');
	await expect(page.locator('.table-wrap')).toBeVisible();

	const hit = await page.evaluate(() => {
		const bar = document.querySelector('.bottom-bar');
		if (bar === null) {
			throw new Error('.bottom-bar not found');
		}
		const box = bar.getBoundingClientRect();
		const target = document.elementFromPoint(
			Math.round(box.x + box.width / 2),
			Math.round(box.y + box.height / 2)
		);
		if (target === null) {
			throw new Error('nothing hit-tested at the bottom bar centre');
		}
		return {
			insideBottomBar: target.closest('.bottom-bar') !== null,
			insideTrackTable: target.closest('.tt-root') !== null
		};
	});

	expect(hit.insideBottomBar).toBe(true);
	expect(hit.insideTrackTable).toBe(false);
});

// PR #1007 discussion r3923591731 (P2/BLOCKING): when a whole-collection
// search returns more than 200 hits, `.truncated-note` renders as a
// non-shrinking sibling of `.table-wrap` INSIDE `.tt-root`'s fixed minimum
// height, so the banner is paid for out of the five rows.
//
// This asserts the fix where the defect actually lives - in the CSS cascade,
// on the real `/performance` page, against the real TrackTable element and
// the real `data-truncated` attribute production writes from
// `provider.truncated`. It drives that attribute directly rather than through
// a truncating search, because no self-contained harness in this repo serves
// a library with more than 200 matching rows; the unit test
// (library-min-5-rows.test.mjs) covers the other half by asserting the
// attribute is bound to `provider.truncated` and that the banner markup is
// gated on the same read. See NOT-verified in the PR body.
// Pin 862cd3 LESS mode: decks 3/4 collapse to 0 height in place, and the
// whole point is that the freed vertical space actually reaches the library
// row (library-min-5-rows.test.mjs proves the CSS source says so; this is
// the render-level proof). At the standard 1280x800 viewport MORE mode
// cannot satisfy both floors at once (see the 969px arithmetic above), but
// LESS mode's own floors are smaller - topbar 28 + wavestack (2 rows) 86 +
// deck floor 248 = 362px, well under 800 - so LESS should get noticeably
// more real library height than MORE at the exact same viewport, while
// decks 1/2 stay unclipped and decks 3/4 stay mounted (just visually
// collapsed, not removed - they keep receiving IPC/audio per the pin).
const LESS_MODE_CHORD = process.platform === 'darwin' ? 'Meta+2' : 'Control+2';

test('performance: switching to LESS frees real height to the library versus MORE, at the same 1280x800 viewport', async ({
	page
}) => {
	await page.setViewportSize(STANDARD_VIEWPORT);
	await page.goto('/performance');

	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible();
	const moreBox = await tableWrap.boundingBox();
	expect(moreBox).not.toBeNull();

	await page.keyboard.press(LESS_MODE_CHORD);
	// The deck-layout transition is CSS-animated (--rb-deck-layout-duration);
	// wait for the class + collapsed deck 3 rather than a fixed timeout.
	await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
	const deck3 = page.locator("[data-deck='3']").first();
	await expect
		.poll(async () => (await deck3.boundingBox())?.height ?? -1)
		.toBeLessThanOrEqual(1);

	const lessBox = await tableWrap.boundingBox();
	expect(lessBox).not.toBeNull();

	// LESS must give the library strictly more room than MORE, at minimum
	// one collapsed deck column's worth (497 - 248 = 249px) - the wavestack
	// shrinking too only ever adds to that margin.
	expect(lessBox!.height - moreBox!.height).toBeGreaterThanOrEqual(249 - 1);

	// Decks 1/2 must stay fully unclipped in LESS, same floor as MORE.
	const deck1 = page.locator('.rb-deck').first();
	await expect(deck1).toBeVisible();
	const deck1Box = await deck1.boundingBox();
	expect(deck1Box).not.toBeNull();
	expect(deck1Box!.height).toBeGreaterThanOrEqual(MIN_DECK_HEIGHT - 1);

	// Decks 3/4 stay MOUNTED (present in the DOM), just visually collapsed -
	// pin 862cd3's whole contract is chrome-only hiding, never unmounting.
	await expect(deck3).toBeAttached();
	const deck4 = page.locator("[data-deck='4']").first();
	await expect(deck4).toBeAttached();
});

test('performance: the truncation banner adds its own height to the 5-row floor', async ({
	page
}) => {
	await page.setViewportSize(TALL_VIEWPORT);
	await page.goto('/performance');

	const table = page.locator('.tt-root');
	await expect(table).toBeVisible();

	const measured = await page.evaluate(() => {
		const root = document.querySelector('.tt-root');
		if (root === null) {
			throw new Error('.tt-root not found');
		}
		const read = () => ({
			minHeight: Number.parseFloat(getComputedStyle(root).minHeight),
			bannerHeight: Number.parseFloat(
				getComputedStyle(root).getPropertyValue('--tt-truncation-note-h')
			)
		});
		const before = read();
		const previous = root.getAttribute('data-truncated');
		root.setAttribute('data-truncated', 'true');
		const after = read();
		root.setAttribute('data-truncated', previous ?? 'false');
		return { before, after };
	});

	expect(measured.before.bannerHeight).toBeGreaterThan(0);
	expect(measured.after.minHeight - measured.before.minHeight).toBeCloseTo(
		measured.before.bannerHeight,
		1
	);
});
