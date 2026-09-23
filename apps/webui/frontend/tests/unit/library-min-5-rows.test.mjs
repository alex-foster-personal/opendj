import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// LIBUX-01 - "Library panel should be slightly greedier - minimum 5 rows of
// songs." Four mechanisms have to agree for this to actually hold on screen:
//
// 1. TrackTable's `.tt-root` carries a min-height floor of thead + 5 rows +
//    a classic-scrollbar-gutter allowance, so flex-shrink cannot crush it
//    below 5 rows when SuggestNextStrip/RecommendedSection also want space
//    (they flex-shrink down to their own floor first - see
//    BrowserPanel.svelte's `.list-panel`), and a horizontal scrollbar on
//    non-overlay-scrollbar platforms (Windows, many Linux themes) cannot
//    eat into that content area either (PR #1007 discussion r3921198996 -
//    table-wrap's default column widths already exceed a short window).
// 2. That floor also grows by the `.truncated-note` banner's own height
//    while the banner is showing, because the banner is a `flex: none`
//    sibling of the scroll area INSIDE the same box and would otherwise be
//    paid for out of the five rows (PR #1007 discussion r3923591731).
// 3. The perf-root grid reserves enough total height, at the ceiling of the
//    deck area, for that floor to fit without overflowing its grid row.
// 4. On a window too short for the reservation to be honored at all - the
//    standard 1280x800 viewport is one, since the deck area's own 497px
//    floor is not reclaimable - `.list-panel` clips, so the shortfall costs
//    visible ROWS and never paints outside the panel or the window
//    (PR #1007 discussion r3921443899).
//
// This test derives the SUM of the real floors from the files that own each
// one (rather than pinning a remembered number) and asserts the perf-root
// reservation is at least that sum - an invariant, so a future edit to any
// one component's chrome height fails this test instead of silently
// reopening the < 5 rows regression.
//
// Regression lines:
// - if `.tt-root`'s min-height is dropped back to 0 (or loses the 5-row
//   term) then TrackTable is crushed to 0 rows on a short window again
// - if the perf-root reservation shrinks below the sum of real floors then
//   the library panel visually overflows the deck area instead of showing
//   5 rows
// - if the floor stops adding the banner height then a truncated
//   whole-collection search shows 4 rows, not 5
// - if `.list-panel` stops clipping then a 1280x800 window paints the track
//   list over the browser bottom bar and 95px outside the window

const PAGE_PATH = fileURLToPath(
	new URL('../../src/routes/performance/+page.svelte', import.meta.url)
);
const TRACK_TABLE_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
);
const BROWSER_PANEL_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);
const SUGGEST_NEXT_STRIP_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/SuggestNextStrip.svelte', import.meta.url)
);
const THEME_PATH = fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url));

const MIN_ROWS = 5;

function firstMatch(source, pattern, label) {
	const m = source.match(pattern);
	assert.ok(m, `expected to find ${label}`);
	return m;
}

// Shared by 'perf-root reserves at least the sum of the real library-chrome
// floors' (MORE) and the PER-MODE test below (LESS) - both need the exact
// same real-component-derived library floor, just checked against a
// different mode's ceiling reservation.
function computeRequiredLibraryPx() {
	const trackTableSource = readFileSync(TRACK_TABLE_PATH, 'utf8');
	const browserPanelSource = readFileSync(BROWSER_PANEL_PATH, 'utf8');
	const suggestNextSource = readFileSync(SUGGEST_NEXT_STRIP_PATH, 'utf8');

	const floorMatch = firstMatch(
		trackTableSource,
		/\.tt-root\s*{[^}]*min-height:\s*calc\((\d+)px\s*\+\s*(\d+)\s*\*\s*var\(--tt-row-h\)\s*\+\s*(\d+)px\s*\+\s*var\(--tt-truncation-h\)\)/,
		'TrackTable .tt-root min-height floor'
	);
	const theadPx = Number(floorMatch[1]);
	const rowFactor = Number(floorMatch[2]);
	const scrollbarGutterPx = Number(floorMatch[3]);
	const cosyRowMatch = firstMatch(
		trackTableSource,
		/\.tt-root\[data-density='cosy'\]\s*{[^}]*--tt-row-h:\s*(\d+)px/,
		"TrackTable's cosy --tt-row-h override"
	);
	const cosyRowPx = Number(cosyRowMatch[1]);

	// min-height since PR #1672: the header row wraps to a second line rather
	// than letting `.list-panel`'s `overflow: hidden` eat its rightmost
	// controls, so 24px is its FLOOR. The floor is what this reservation needs
	// - a taller header costs library rows, which is the documented
	// degradation, not a violated budget.
	const paneHeaderMatch = firstMatch(
		browserPanelSource,
		/\.pane-header\s*{[^}]*min-height:\s*(\d+)px/,
		'BrowserPanel .pane-header min-height floor'
	);
	const paneHeaderPx = Number(paneHeaderMatch[1]);

	const bottomBarMatch = firstMatch(
		browserPanelSource,
		/grid-template-rows:\s*minmax\(0,\s*1fr\)\s*(\d+)px/,
		"BrowserPanel .rb-browser's bottom-bar row height"
	);
	const bottomBarPx = Number(bottomBarMatch[1]);

	const stripFloorMatch = firstMatch(
		suggestNextSource,
		/\.strip\s*{[^}]*min-height:\s*(\d+)px/,
		'SuggestNextStrip .strip min-height floor'
	);
	const stripFloorPx = Number(stripFloorMatch[1]);

	const truncationNoteMatch = firstMatch(
		trackTableSource,
		/--tt-truncation-note-h:\s*(\d+)px/,
		"TrackTable's declared .truncated-note height"
	);
	const truncationNotePx = Number(truncationNoteMatch[1]);

	const requiredLibraryPx =
		paneHeaderPx +
		stripFloorPx +
		theadPx +
		rowFactor * cosyRowPx +
		scrollbarGutterPx +
		truncationNotePx;

	return { requiredLibraryPx, bottomBarPx };
}

test('TrackTable.tt-root has a 5-row min-height floor keyed to --tt-row-h', () => {
	const source = readFileSync(TRACK_TABLE_PATH, 'utf8');
	const m = firstMatch(
		source,
		/\.tt-root\s*{[^}]*min-height:\s*calc\((\d+)px\s*\+\s*(\d+)\s*\*\s*var\(--tt-row-h\)\s*\+\s*(\d+)px\s*\+\s*var\(--tt-truncation-h\)\)/,
		'a `.tt-root { min-height: calc(<thead>px + N * var(--tt-row-h) + <gutter>px + var(--tt-truncation-h)) }` floor'
	);
	const rowFactor = Number(m[2]);
	assert.ok(
		rowFactor >= MIN_ROWS,
		`.tt-root min-height must reserve at least ${MIN_ROWS} rows, found ${rowFactor}`
	);
});

test('perf-root reserves at least the sum of the real library-chrome floors', () => {
	const pageSource = readFileSync(PAGE_PATH, 'utf8');
	const { requiredLibraryPx, bottomBarPx } = computeRequiredLibraryPx();

	const reserveMatch = firstMatch(
		pageSource,
		/100vh - var\(--rb-topbar-h\) -\s*4 \* \(var\(--rb-waverow-h\) \+ var\(--rb-stemwave-stack-extra, 0px\)\) -\s*(\d+)px/,
		"perf-root's deck-area ceiling reservation"
	);
	const reservedPx = Number(reserveMatch[1]);

	assert.ok(
		reservedPx >= requiredLibraryPx + bottomBarPx,
		`perf-root (MORE) reserves ${reservedPx}px for the library panel, but the real ` +
			`chrome floors need >= ${requiredLibraryPx + bottomBarPx}px ` +
			`(library floor ${requiredLibraryPx} + bottom-bar ${bottomBarPx})`
	);
});

// PR #1007 discussion r3923591731 (P2/BLOCKING): `.truncated-note` renders
// inside `.tt-root` as a `flex: none` sibling of the scroll area whenever a
// whole-collection search hits its 200-row safety cap, so unless the floor
// grows by exactly the banner's height, the banner is paid for out of the
// five rows - one whole cosy row on a platform with classic scrollbars. Three
// things have to line up, and each is asserted separately so a partial edit
// cannot pass: the banner has a DECLARED height (a font-metric height cannot
// be added to a static budget), the floor adds that height only while the
// banner is showing, and the markup drives that state from the real
// `provider.truncated` read rather than from anything test-only.
test('the truncation banner has a declared height that the floor can add', () => {
	const source = readFileSync(TRACK_TABLE_PATH, 'utf8');

	const declared = firstMatch(
		source,
		/--tt-truncation-note-h:\s*(\d+)px/,
		'a declared --tt-truncation-note-h'
	);
	assert.ok(
		Number(declared[1]) > 0,
		'the truncation banner height must be a real positive height, not 0'
	);

	// `.truncated-note` must actually BE that tall, border-box, or the number
	// the floor adds and the number the banner occupies drift apart.
	const noteRule = firstMatch(source, /\.truncated-note\s*{([^}]*)}/, 'a .truncated-note rule');
	assert.match(
		noteRule[1],
		/height:\s*var\(--tt-truncation-note-h\)/,
		'.truncated-note must take its height from --tt-truncation-note-h'
	);
	assert.match(
		noteRule[1],
		/box-sizing:\s*border-box/,
		'.truncated-note must be border-box so its border and padding stay inside the declared height'
	);
});

test('the floor adds the banner height only while the banner is showing', () => {
	const source = readFileSync(TRACK_TABLE_PATH, 'utf8');

	assert.match(
		source,
		/\.tt-root\s*{[^}]*--tt-truncation-h:\s*0px/,
		'the default --tt-truncation-h must be 0px, so a non-truncated pane does not steal deck height'
	);
	assert.match(
		source,
		/\.tt-root\[data-truncated='true'\]\s*{[^}]*--tt-truncation-h:\s*var\(--tt-truncation-note-h\)/,
		"a `.tt-root[data-truncated='true']` rule must raise --tt-truncation-h to the banner height"
	);
});

test('the truncated flag on .tt-root is driven by the real provider read', () => {
	const source = readFileSync(TRACK_TABLE_PATH, 'utf8');

	assert.match(
		source,
		/data-truncated=\{provider\.truncated \? 'true' : 'false'\}/,
		'.tt-root must set data-truncated from provider.truncated (the same read that renders the banner)'
	);
	// The banner itself is still gated on the same read - if these two ever
	// diverge, the floor would grow without a banner or vice versa.
	assert.match(
		source,
		/\{#if provider\.truncated\}/,
		'the .truncated-note markup must stay gated on provider.truncated'
	);
});

// PR #1007 discussion r3921443899 (P2/BLOCKING): the floor above is ABSOLUTE,
// so on a window too short to honor it `.tt-root` keeps its floor height and
// paints outside the panel. Measured on main at the repo's standard 1280x800
// /performance viewport before this fix: `.tt-root` ran to y=869 in an 800px
// window and `.perf-root` reported scrollHeight 895 vs clientHeight 800.
// `.list-panel` must therefore clip, so a short window costs visible ROWS and
// never the panel's own chrome or the window's bounds. The e2e counterpart
// (performance-library-min-rows.spec.ts) proves it in a real browser; this
// asserts the declaration cannot be dropped without a test going red.
test('BrowserPanel .list-panel clips, so a short window cannot push the table off-window', () => {
	const source = readFileSync(BROWSER_PANEL_PATH, 'utf8');

	const listPanelRule = firstMatch(source, /\.list-panel\s*{([^}]*)}/, 'a .list-panel rule');
	assert.match(
		listPanelRule[1],
		/overflow:\s*hidden/,
		'.list-panel must set overflow: hidden so the absolute min-height floor in TrackTable cannot paint outside it'
	);
});

// PR #1007 discussion r3921321752 (P1/BLOCKING): the library's 255px
// reservation above pushes the deck-area below its own documented
// content-tight floor (248px/deck, 497px for a two-deck column) at the
// repo's standard 1280x800 viewport, and `.rb-deck` uses overflow: hidden so
// a shorter box genuinely clips controls. The deck-area grid track must
// therefore carry `minmax(<floor>px, ...)` so it can never be squeezed below
// that floor by any reservation value above it - this derives the floor from
// the comment's own per-deck/two-deck numbers (arithmetic self-consistency)
// and asserts the CSS is at least that, rather than pinning a remembered 497.
//
// Pin 246b0f5 / Sol P1 finding (comment 3963232872, BLOCKING): the same row
// is ALSO `<Mixer />`'s row (`.deck-area`'s grid-template-areas), and MORE
// mode's un-collapsed mixer content (30px EQs + FILTER, `flex-shrink: 0`)
// needs MORE than the deck's own 497px - so the floor is no longer required
// to equal columnPx exactly, only to be at least it (>=), same as LESS mode
// already was allowed to exceed its own per-deck floor for the identical
// mixer-driven reason. channel-strip-less-floor.test.mjs's "MORE floor" test
// derives and checks the real mixer-driven number this floor must cover.
test('deck-area grid track has a minmax floor at least the documented two-deck-column height', () => {
	const pageSource = readFileSync(PAGE_PATH, 'utf8');

	const perDeckMatch = firstMatch(
		pageSource,
		/one deck needs (\d+)px/,
		'the documented per-deck content-tight height'
	);
	const perDeckPx = Number(perDeckMatch[1]);

	const columnMatch = firstMatch(
		pageSource,
		/two-deck column needs (\d+)px/,
		'the documented two-deck-column height'
	);
	const columnPx = Number(columnMatch[1]);

	const gapMatch = firstMatch(pageSource, /\.deck-col\s*{[^}]*gap:\s*(\d+)px/, '.deck-col gap');
	const gapPx = Number(gapMatch[1]);

	assert.equal(
		columnPx,
		perDeckPx * 2 + gapPx,
		`documented two-deck-column height (${columnPx}) must equal 2 * per-deck height ` +
			`(${perDeckPx}) + .deck-col gap (${gapPx})`
	);

	const deckFloorMatch = firstMatch(
		pageSource,
		/grid-template-rows:[\s\S]*?minmax\(\s*(\d+)px,\s*\n\s*min\(/,
		"the deck-area track's minmax floor"
	);
	const deckFloorPx = Number(deckFloorMatch[1]);

	assert.ok(
		deckFloorPx >= columnPx,
		`deck-area minmax floor (${deckFloorPx}) must be at least the documented two-deck-column ` +
			`height (${columnPx}) so LIBUX-01's library reservation can never clip deck controls`
	);
});

// Pin 862cd3 LESS mode: decks 3/4 collapse to 0 height in place (chrome
// only, both components stay mounted), and the whole point of the pin is
// that the LIBRARY gains the freed vertical space. That only happens if
// the OUTER `.perf-root.deck-layout-less` grid track floors shrink to
// match the collapsed content - otherwise the freed component height is
// trapped inside grid tracks still sized for MORE mode, and the library
// gains nothing (the regression this test guards against). Both modes'
// floors are derived from the same source-of-truth comments/values rather
// than pinned as remembered numbers, so a future edit to either mode's
// numbers fails this test instead of silently reopening the bug.
test('perf-root has PER-MODE row floors, and LESS reserves less than MORE by at least one collapsed deck', () => {
	const pageSource = readFileSync(PAGE_PATH, 'utf8');
	const themeSource = readFileSync(THEME_PATH, 'utf8');

	const waverowMatch = firstMatch(
		themeSource,
		/--rb-waverow-h:\s*(\d+)px/,
		"theme.css's --rb-waverow-h value"
	);
	const waverowPx = Number(waverowMatch[1]);

	const perDeckMatch = firstMatch(
		pageSource,
		/one deck needs (\d+)px/,
		'the documented per-deck content-tight height'
	);
	const perDeckPx = Number(perDeckMatch[1]);

	const columnMatch = firstMatch(
		pageSource,
		/two-deck column needs (\d+)px/,
		'the documented two-deck-column height'
	);
	const columnPx = Number(columnMatch[1]);

	// Scope each mode's grid-template-rows to its own rule block. Neither
	// `.perf-root { ... }` nor `.perf-root.deck-layout-less { ... }` contains
	// a nested `{`, so a plain "up to the next closing brace" capture is
	// exact - and `\.perf-root\s*\{` cannot accidentally match a compound
	// selector like `.perf-root.tw-active {`, since the character right
	// after "root" there is `.`, not whitespace or `{`.
	const moreBlockMatch = firstMatch(
		pageSource,
		/\.perf-root\s*\{([^}]*)\}/,
		'the default (MORE) .perf-root rule block'
	);
	const lessBlockMatch = firstMatch(
		pageSource,
		/\.perf-root\.deck-layout-less\s*\{([^}]*)\}/,
		'the .perf-root.deck-layout-less override rule block'
	);
	const moreBlock = moreBlockMatch[1];
	const lessBlock = lessBlockMatch[1];

	function wavestackRows(block, label) {
		const m = firstMatch(
			block,
			/calc\((\d+)\s*\*\s*\(var\(--rb-waverow-h\)\s*\+\s*var\(--rb-stemwave-stack-extra,\s*0px\)\)\)/,
			`a wavestack row count in ${label}`
		);
		return Number(m[1]);
	}
	function deckFloorPx(block, label) {
		const m = firstMatch(block, /minmax\(\s*(\d+)px,/, `a deck-area minmax floor in ${label}`);
		return Number(m[1]);
	}

	const moreWavestackRows = wavestackRows(moreBlock, 'MORE');
	const lessWavestackRows = wavestackRows(lessBlock, 'LESS');
	const moreDeckFloor = deckFloorPx(moreBlock, 'MORE');
	const lessDeckFloor = deckFloorPx(lessBlock, 'LESS');

	// MORE's wavestack is unchanged from before the pin (full 4-row).
	assert.equal(moreWavestackRows, 4, 'MORE mode must reserve all 4 wavestack rows');
	// Sol P1 finding (comment 3963232872, BLOCKING), round 1: MORE's
	// deck-area floor used to equal columnPx exactly, because the deck
	// column was the only thing sharing this grid row with a real height
	// requirement. Round 1 grew it to 524px because MORE mode's
	// un-collapsed mixer content (30px EQs + FILTER, `flex-shrink: 0`)
	// needed more than the deck's own 497px. Round 3 then compacted the
	// strip's own margins (see channel-strip-less-floor.test.mjs's "MORE
	// floor" test) so that content fits back inside 497px, and restored the
	// floor to exactly 497px - so `>= columnPx` now holds with the two
	// equal, not because a mixer-driven number still exceeds it. The
	// assertion is kept as `>=` rather than `==` so a future mixer-driven
	// requirement can grow the floor again without failing this test.
	assert.ok(
		moreDeckFloor >= columnPx,
		`MORE deck-area floor (${moreDeckFloor}) must be at least the two-deck-column height (${columnPx})`
	);

	// LESS only ever shows one deck per column (3/4 collapsed), so it needs
	// only 2 wavestack rows.
	assert.equal(lessWavestackRows, 2, 'LESS mode must reserve only 2 wavestack rows (decks 1/2)');
	// Pin 246b0f5 (follow-on to 862cd3): the LESS deck-area floor used to
	// equal perDeckPx exactly, because the deck column was the only thing
	// sharing this grid row with a real height requirement. It no longer is
	// - `<Mixer />` shares the SAME row (`.deck-area`'s grid-template-areas
	// is 'decks-left mixer decks-right', one row), and pin 246b0f5 found the
	// mixer's own LESS-mode content (decks 1/2's compacted channel strip,
	// still un-collapsed - only strips 3/4's WIDTH goes to 0) needs MORE
	// height than one deck panel alone, to avoid `.rb-mixer`'s
	// `overflow: hidden` clipping the fader/level-meter/EQ/STEM controls off
	// the bottom - exactly the maintainer's "you ddin't move the 1/2 levels" report.
	// So the floor is now >= perDeckPx (the deck's own requirement is still
	// respected) rather than exactly perDeckPx; the precise mixer-driven
	// number (401, including the mixer's own chrome - Sol P1 finding,
	// comment 3963232874) is pinned and derived from the real component CSS
	// in channel-strip-less-floor.test.mjs, not duplicated here.
	assert.ok(
		lessDeckFloor >= perDeckPx,
		`LESS deck-area floor (${lessDeckFloor}) must be at least the one-deck height (${perDeckPx}) ` +
			'so decks 1/2 are never squeezed below their own content-tight floor'
	);

	// The library row is `minmax(0, 1fr)` in both modes (unchanged), so it
	// picks up whatever the topbar/wavestack/deckarea rows above it do not
	// reserve. Compare that reservation directly: LESS must still reserve
	// meaningfully less than MORE (a real gain to the library), though pin
	// 246b0f5 no longer guarantees a full collapsed-deck-column's worth -
	// see the lessDeckFloor comment above. the maintainer's own words authorizing
	// this trade: "probably a little bit more height taken from library ...
	// but optimize it visually" (pin 246b0f5).
	const moreReservedRows = moreWavestackRows * waverowPx + moreDeckFloor;
	const lessReservedRows = lessWavestackRows * waverowPx + lessDeckFloor;
	const libraryGainPx = moreReservedRows - lessReservedRows;

	// A bare `> 0` here would pass a 1px gain, which defeats the point of
	// this test - it exists to catch a future change that shrinks LESS's
	// library benefit back toward nothing. The achievable gain today is
	// `(moreWavestackRows - lessWavestackRows) * waverowPx + (moreDeckFloor -
	// lessDeckFloor)` = (4 - 2) * 43 + (497 - 401) = 182px (all four terms
	// read from source above, not hand-typed; round 3 restored MORE's floor
	// to the original 497px - see +page.svelte's LIBUX-01 comment - while
	// keeping LESS's floor at 401px - issue #1578's fix, one px above the
	// prior Sol-P1-fixed 400px, to also cover the R/M `.cal-controls` row -
	// so this is neither the round-1 524/210 figures nor the original
	// pre-pin 497/388 pair).
	// MIN_LESS_LIBRARY_GAIN is pinned well under that (150px), leaving 32px
	// of real headroom for a future legitimate shrink (e.g. a further
	// mixer-height adjustment) while still catching a regression toward a
	// token few-px "gain".
	const MIN_LESS_LIBRARY_GAIN = 150;
	assert.ok(
		libraryGainPx >= MIN_LESS_LIBRARY_GAIN,
		`LESS must free at least ${MIN_LESS_LIBRARY_GAIN}px to the library versus MORE, but MORE ` +
			`reserves ${moreReservedRows}px and LESS reserves ${lessReservedRows}px (gain ${libraryGainPx}px)`
	);

	// (a) Freeing space to the library is pointless if LESS's own ceiling
	// reservation (the "how much am I leaving for the library" term inside
	// its minmax/min calc) is not itself enough to cover the library's real
	// 5-row floor - assert that holds for LESS specifically, not just MORE
	// (test 2 above already covers MORE).
	const { requiredLibraryPx, bottomBarPx } = computeRequiredLibraryPx();
	const lessCeilingReserveMatch = firstMatch(
		lessBlock,
		/100vh - var\(--rb-topbar-h\) -\s*2 \* \(var\(--rb-waverow-h\) \+ var\(--rb-stemwave-stack-extra, 0px\)\) -\s*(\d+)px/,
		"LESS mode's deck-area ceiling reservation"
	);
	const lessCeilingReservePx = Number(lessCeilingReserveMatch[1]);

	assert.ok(
		lessCeilingReservePx >= requiredLibraryPx + bottomBarPx,
		`perf-root (LESS) reserves ${lessCeilingReservePx}px for the library panel, but the real ` +
			`chrome floors need >= ${requiredLibraryPx + bottomBarPx}px ` +
			`(library floor ${requiredLibraryPx} + bottom-bar ${bottomBarPx})`
	);
});
