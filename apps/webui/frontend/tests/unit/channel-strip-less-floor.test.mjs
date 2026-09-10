import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// Pin 246b0f53f725, follow-on to pin 862cd3 (58a16ac781db) - the maintainer's words:
// "you ddin't move the 1/2 levels so now can't be seen in LESS (requirement
// not to cover required dials etc plz for LESS)... MORE/LESS toggle is too
// big. pushing EQs down. EQs now don't fit and need adjusting to work +
// probably a little bit more height taken from library, ubt optimize it
// visually."
//
// `.deck-area`'s grid-template-areas is 'decks-left mixer decks-right' - a
// SINGLE row - so `<Mixer />` shares +page.svelte's deck-area floor with the
// deck columns, in BOTH modes. This file derives the mixer's real height
// requirement, LESS and MORE, from ChannelStrip.svelte/Mixer.svelte/
// theme.css's own source (the same style library-min-5-rows.test.mjs uses
// for the deck/library floors) and asserts +page.svelte's floor for each
// mode actually covers it, rather than pinning a remembered number. The
// render-level proof (a real browser, not just source text) is
// performance-less-mode-mixer-levels.spec.ts.
//
// - LESS: before pin 246b0f5 the floor was sized only for one deck panel
//   (248px); the mixer's own LESS-mode content needs more than that, and
//   `.rb-mixer { overflow: hidden }` silently clipped the excess - the
//   fader and its ChannelLevelMeter, being the LAST elements in the flex
//   column, were the first casualty.
// - MORE: Sol P1 finding on pin 246b0f5 (comment 3963232872, BLOCKING) -
//   pin 246b0f5 also made every non-fader strip child `flex-shrink: 0`
//   (so a too-short strip overflows visibly instead of squeezing), but
//   never checked MORE mode's OWN un-collapsed content (30px EQs + FILTER)
//   against the deck-area floor it shares with the deck columns. It
//   overflowed there too.
// - Both modes: Sol P1 finding (comment 3963232874, BLOCKING) - the
//   original LESS floor (388px) covered only toggle+strip+lower and left
//   `.rb-mixer`'s own 12px of padding/border chrome uncounted, clipping the
//   last ~12px. Both modes' required-floor calculations below add that
//   chrome back in.
//
// Four numbers below (TOGGLE_PX, CH_NUM_PX, CUE_BTN_PX, STEM_SLOT_PX) are
// measured constants, not regex-derived: they are rendered heights driven by
// browser font-metric line-height, which no static CSS regex can predict
// exactly (the same reason +page.svelte's "one deck needs 248px" and
// "library needs 272px" floors are themselves documented, measured
// arithmetic rather than fully re-derived from font metrics). They are
// unchanged by this pin (STEM/CUE/ch-num are not touched by the `less`
// prop), measured Playwright/Chromium, 1280x800 - see
// performance-less-mode-mixer-levels.spec.ts for the live equivalent.
const TOGGLE_PX = 17;
const CH_NUM_PX = 10;
const CUE_BTN_PX = 14;
const STEM_SLOT_PX = 54;
const STEM_LABEL_PX = 8;
const LOWER_PX = 76;
// Issue #1578: `.strip-head` is a flex ROW holding `.ch-num` AND the R/M
// `.cal-controls` group (#1475) side by side, in BOTH modes - #1475 landed
// after CH_NUM_PX above was measured and never re-measured the row. A flex
// row's cross-axis (height) size is the tallest child regardless of
// align-items, so `.strip-head`'s real height is max(CH_NUM_PX,
// CAL_CONTROLS_PX), not the bare CH_NUM_PX both floor tests assumed - a 1px
// understatement in LESS mode, where the floor has no slack. Measured the
// same way as the other constants above (Playwright/Chromium, isolated
// fixture with ChannelStrip.svelte's exact `.strip-head`/`.cal-controls`/
// `.cal-btn` CSS): { head: 11, chnum: 10, cal: 11, calBtn: 11 }. The chnum
// reading is a positive control - it reproduces the already-recorded
// CH_NUM_PX (10) exactly, which is what makes the cal reading trustworthy
// despite the fixture's hand-copied CSS and missing Inter/SF Pro font.
const CAL_CONTROLS_PX = 11;

const CHANNEL_STRIP_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/mixer/ChannelStrip.svelte', import.meta.url)
);
const KNOB_PATH = fileURLToPath(new URL('../../src/lib/components/rb/mixer/Knob.svelte', import.meta.url));
const MIXER_PATH = fileURLToPath(new URL('../../src/lib/components/rb/Mixer.svelte', import.meta.url));
const PAGE_PATH = fileURLToPath(new URL('../../src/routes/performance/+page.svelte', import.meta.url));
const THEME_PATH = fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url));

function firstMatch(source, pattern, label) {
	const m = source.match(pattern);
	assert.ok(m, `expected to find ${label}`);
	return m;
}

// Pin 2917b0eca218 - "Cant currently see filter in LESS mode." FILTER used
// to be unmounted in LESS (`{#if !less}`) because a single vertical stack
// could not afford its height. It is now SHRUNK like TRIM and the EQs, and
// `.strip.less` is a grid that only needs its dial COLUMN to be tall.
test('ChannelStrip.svelte keeps FILTER in both modes and shrinks every dial in LESS', () => {
	const src = readFileSync(CHANNEL_STRIP_PATH, 'utf8');
	assert.doesNotMatch(
		src,
		/\{#if !less\}/,
		'no control may be unmounted by LESS mode any more - the grid makes room for all of them'
	);
	assert.match(src, /size=\{trimSize\}/, 'TRIM must be driven by the derived trimSize');
	assert.match(src, /size=\{eqSize\}/, 'HI/MID/LOW must be driven by the derived eqSize');
	assert.match(src, /size=\{filterSize\}/, 'FILTER must be driven by the derived filterSize');
	// EVERY child, not the four the first draft covered. CSS grid auto-places
	// an unplaced item into the first open cell in DOM order, so with only ONE
	// `grid-area` missing the layout still looks right by elimination - but
	// drop two and the earlier child takes the earlier hole. `trim-slot`
	// precedes `cue-btn` in the markup, so losing both areas silently SWAPS
	// TRIM and CUE with nothing to catch it. Blinded review, Thu 10 Sep 2026.
	for (const [name, area] of [
		['strip-head', 'head'],
		['cue-btn', 'cue'],
		['trim-slot', 'trim'],
		['stem-label', 'stemlabel'],
		['fader-slot', 'fader'],
		['eq-stack', 'eq'],
		['stem-slot', 'stem'],
		['filter-slot', 'filter']
	]) {
		assert.match(
			src,
			new RegExp(`\\.strip\\.less \\.${name}\\s*\\{[^}]*grid-area:\\s*${area}`),
			`.${name} must be placed in the LESS grid`
		);
	}
	// the maintainer asked for the slider and buttons LEFT and RIGHT of the EQs. The
	// rendered proof is performance-less-mode-mixer-levels.spec.ts; this only
	// pins the intent so the areas cannot be silently reordered into a stack.
	const areas = firstMatch(src, /grid-template-areas:\s*([^;]*);/, 'the LESS grid-template-areas');
	const rows = areas[1].trim().split('\n').map((row) => row.trim().replace(/'/g, '').split(/\s+/));
	assert.deepEqual(
		rows,
		[
			['head', 'head', 'head'],
			['cue', 'trim', 'stemlabel'],
			['fader', 'eq', 'stem'],
			['fader', 'filter', 'stem']
		],
		'the whole LESS grid is the contract, not just the row the EQs sit in'
	);
	const dialRow = rows.find((row) => row.includes('eq'));
	assert.ok(dialRow, 'one grid row must hold the EQ stack');
	assert.equal(dialRow[0], 'fader', 'the channel fader belongs LEFT of the EQs');
	assert.equal(dialRow[2], 'stem', 'the STEM buttons belong RIGHT of the EQs');
	// Every named area is used by exactly one child, so no child can be left
	// to auto-place into an implicit row.
	const areaNames = new Set(rows.flat());
	assert.equal(areaNames.size, 8, 'the LESS grid names exactly eight areas');
});

test('.strip fixes every non-fader child so a too-short strip overflows visibly instead of squeezing', () => {
	const src = readFileSync(CHANNEL_STRIP_PATH, 'utf8');
	const rule = firstMatch(
		src,
		/\.strip > :not\(\.fader-slot\)\s*\{([^}]*)\}/,
		'a `.strip > :not(.fader-slot)` rule'
	);
	assert.match(rule[1], /flex-shrink:\s*0/, 'every fixed-size strip child must refuse to shrink');
});

// Shared by both mode tests below - reads every non-mode-specific number
// straight from ChannelStrip.svelte's source once, so LESS/MORE cannot
// silently read two different versions of the same constant.
function readSharedStripNumbers() {
	const stripSrc = readFileSync(CHANNEL_STRIP_PATH, 'utf8');
	const knobSrc = readFileSync(KNOB_PATH, 'utf8');

	// Knob's own caption formula (shared by every dial size): dial diameter
	// (the `size` prop) + a 1px caption gap + 8px caption text.
	const gapMatch = firstMatch(knobSrc, /--knob-caption-gap: (\d+)px/, "Knob's caption gap");
	const captionMatch = firstMatch(knobSrc, /--knob-caption-size: (\d+)px/, "Knob's caption size");
	const captionExtraPx = Number(gapMatch[1]) + Number(captionMatch[1]);
	const knobBoxPx = (size) => size + captionExtraPx;

	// ANCHORED to the start of a line, here and everywhere else a BASE rule
	// is read: `.strip.less .eq-stack { grid-area: eq; }` (pin 2917b0eca218)
	// also contains the text `.eq-stack {`, and an unanchored search would
	// read the descendant rule's body instead of the base rule's.
	const eqGapMatch = firstMatch(stripSrc, /^\t\.eq-stack\s*\{[^}]*gap:\s*(\d+)px/m, '.eq-stack gap');
	const eqGapPx = Number(eqGapMatch[1]);

	const faderMinMatch = firstMatch(
		stripSrc,
		/^\t\.fader-slot\s*\{[^}]*min-height:\s*(\d+)px/m,
		'.fader-slot min-height floor'
	);
	const faderMinPx = Number(faderMinMatch[1]);

	const stripFlexGapMatch = firstMatch(stripSrc, /\.strip\s*\{[^}]*gap:\s*(\d+)px/, '.strip flex gap');
	const stripFlexGapPx = Number(stripFlexGapMatch[1]);

	const stripPaddingMatch = firstMatch(
		stripSrc,
		/\.strip\s*\{[^}]*padding:\s*(\d+)px\s+(\d+)px\s+(\d+)px/,
		'.strip padding'
	);
	const stripPaddingPx = Number(stripPaddingMatch[1]) + Number(stripPaddingMatch[3]);

	return { stripSrc, knobBoxPx, eqGapPx, faderMinPx, stripFlexGapPx, stripPaddingPx };
}

// `.rb-mixer`'s own vertical chrome, uncounted by the original 388px LESS
// floor (Sol P1 finding, comment 3963232874): Mixer.svelte's own
// `padding: 6px 6px 4px` (top+bottom = vertical padding) plus the 1px-per-edge
// border every `.rb-panel` gets from theme.css. Both count against the
// deck-area track's floor because the whole app runs box-sizing: border-box
// (app.css), so the mixer's flex column (toggle+strip+lower) only gets
// floor - chrome, not floor.
function readMixerChromePx() {
	const mixerSrc = readFileSync(MIXER_PATH, 'utf8');
	const themeSrc = readFileSync(THEME_PATH, 'utf8');

	const paddingMatch = firstMatch(
		mixerSrc,
		/\.rb-mixer\s*\{[^}]*padding:\s*(\d+)px\s+(\d+)px\s+(\d+)px/,
		'.rb-mixer padding'
	);
	const paddingVerticalPx = Number(paddingMatch[1]) + Number(paddingMatch[3]);

	const borderMatch = firstMatch(
		themeSrc,
		/\.perf-root \.rb-panel\s*\{[^}]*border:\s*(\d+)px solid/,
		'.rb-panel border width'
	);
	const borderVerticalPx = Number(borderMatch[1]) * 2; // top + bottom edge

	return paddingVerticalPx + borderVerticalPx;
}

// Reads the deck-area minmax floor for a given mode's rule block in
// +page.svelte ('MORE' = the default `.perf-root` block, 'LESS' =
// `.perf-root.deck-layout-less`).
function readDeckAreaFloorPx(pageSrc, mode) {
	const pattern =
		mode === 'LESS'
			? /\.perf-root\.deck-layout-less\s*\{[\s\S]*?minmax\(\s*(\d+)px,/
			: /\.perf-root\s*\{(?!\.deck-layout-less)[\s\S]*?minmax\(\s*(\d+)px,/;
	const m = firstMatch(pageSrc, pattern, `the ${mode} deck-area minmax floor`);
	return Number(m[1]);
}

test('the mixer LESS floor (+page.svelte) covers the real ChannelStrip LESS-mode content height', () => {
	const { stripSrc, knobBoxPx, eqGapPx, faderMinPx, stripFlexGapPx, stripPaddingPx } = readSharedStripNumbers();
	const pageSrc = readFileSync(PAGE_PATH, 'utf8');
	const mixerChromePx = readMixerChromePx();

	const lessTrimMatch = firstMatch(stripSrc, /const LESS_TRIM_SIZE = (\d+(?:\.\d+)?);/, 'LESS_TRIM_SIZE');
	const lessEqMatch = firstMatch(stripSrc, /const LESS_EQ_SIZE = (\d+(?:\.\d+)?);/, 'LESS_EQ_SIZE');
	const lessTrimSize = Number(lessTrimMatch[1]);
	const lessEqSize = Number(lessEqMatch[1]);

	function lessMarginPx(selector, label) {
		const rule = firstMatch(
			stripSrc,
			new RegExp(`\\.strip\\.less ${selector.replace('.', '\\.')}\\s*\\{([^}]*)\\}`),
			`a .strip.less ${label} override`
		);
		const marginMatch = firstMatch(rule[1], /margin-(?:top|bottom):\s*(\d+)px/, `${label}'s LESS margin`);
		return Number(marginMatch[1]);
	}

	const trimMarginPx = lessMarginPx('.trim-slot', 'trim-slot');
	const cueMarginPx = lessMarginPx('.cue-btn', 'cue-btn');
	const faderMarginPx = lessMarginPx('.fader-slot', 'fader-slot');
	const stemLabelMarginPx = lessMarginPx('.stem-label', 'stem-label');

	const filterMarginPx = (() => {
		const rule = firstMatch(stripSrc, /^\t\.filter-slot\s*\{([^}]*)\}/m, 'a .filter-slot rule');
		const top = firstMatch(rule[1], /margin-top:\s*(\d+)px/, "filter-slot's margin-top");
		const bottom = firstMatch(rule[1], /margin-bottom:\s*(\d+)px/, "filter-slot's margin-bottom");
		return Number(top[1]) + Number(bottom[1]);
	})();
	const lessFilterMatch = firstMatch(
		stripSrc,
		/const LESS_FILTER_SIZE = (\d+(?:\.\d+)?);/,
		'LESS_FILTER_SIZE'
	);
	const lessFilterSize = Number(lessFilterMatch[1]);

	// Pin 2917b0eca218: `.strip.less` is a GRID, not a flex column, so the
	// requirement is no longer the SUM of every child - it is the sum of the
	// grid's four ROW heights, each of which is the tallest item in that row.
	// That is the whole point of the change: only the dial column has to be
	// tall, and the fader and STEM columns ride alongside it.
	//
	//   'head  head   head'       <- row 1
	//   'cue   trim   stemlabel'  <- row 2
	//   'fader eq     stem'       <- row 3   (fader/stem span rows 3-4)
	//   'fader filter stem'       <- row 4
	const headRowPx = Math.max(CH_NUM_PX, CAL_CONTROLS_PX);
	const secondRowPx = Math.max(
		CUE_BTN_PX + cueMarginPx,
		knobBoxPx(lessTrimSize) + trimMarginPx,
		STEM_LABEL_PX + stemLabelMarginPx
	);
	const eqRowPx = 3 * knobBoxPx(lessEqSize) + 2 * eqGapPx;
	const filterRowPx = knobBoxPx(lessFilterSize) + filterMarginPx;
	const ROW_COUNT_LESS = 4;
	const rowGapsTotalPx = (ROW_COUNT_LESS - 1) * stripFlexGapPx;

	// The fader and STEM columns span rows 3+4, so they only grow the strip
	// if their own floors exceed what those two rows already provide. Asserted
	// rather than assumed: if a future edit makes the fader taller than the
	// dial column, this arithmetic would silently understate the requirement.
	const spannedRowsPx = eqRowPx + filterRowPx + stripFlexGapPx;
	const faderColumnPx = faderMinPx + faderMarginPx;
	assert.ok(
		spannedRowsPx >= Math.max(faderColumnPx, STEM_SLOT_PX),
		`the dial column (${spannedRowsPx}px over rows 3-4) must still be the tallest thing in ` +
			`the LESS grid - fader needs ${faderColumnPx}px, STEM needs ${STEM_SLOT_PX}px - or ` +
			'the row heights below stop describing the strip'
	);

	const stripContentPx =
		headRowPx + secondRowPx + eqRowPx + filterRowPx + rowGapsTotalPx + stripPaddingPx;

	const requiredMixerPx = TOGGLE_PX + stripContentPx + LOWER_PX + mixerChromePx;

	const lessFloorPx = readDeckAreaFloorPx(pageSrc, 'LESS');

	assert.ok(
		lessFloorPx >= requiredMixerPx,
		`+page.svelte's LESS deck-area floor (${lessFloorPx}px) must be >= the mixer's real LESS-mode ` +
			`content requirement (${requiredMixerPx}px = toggle ${TOGGLE_PX} + strip ${stripContentPx} ` +
			`+ lower ${LOWER_PX} + chrome ${mixerChromePx}), or the fader/level-meter/EQ/STEM controls clip again`
	);

	// The documented breakdown comment in +page.svelte must not silently
	// drift from what the CSS actually enforces. LESS's numbers happen to
	// be whole and sum exactly, so this checks strict equality (MORE's
	// don't - see the MORE test below).
	const commentMatch = firstMatch(
		pageSrc,
		/toggle (\d+) \+ strip (\d+) \+ lower (\d+) \+ chrome (\d+) = (\d+)px/,
		'the documented toggle+strip+lower+chrome=N breakdown comment'
	);
	assert.equal(
		Number(commentMatch[5]),
		Number(commentMatch[1]) + Number(commentMatch[2]) + Number(commentMatch[3]) + Number(commentMatch[4])
	);
	assert.equal(
		Number(commentMatch[5]),
		lessFloorPx,
		'the documented breakdown total must equal the actual CSS floor, or the comment has drifted'
	);
	// Pin 2917b0eca218 closed a hole here: the four terms were only checked
	// against each other and against the floor, never against what the strip
	// actually needs. So a strip that got shorter left "strip 296" standing as
	// a documented fact about nothing while every assertion stayed green -
	// which is what happened when `.strip.less` became a grid.
	assert.equal(
		Number(commentMatch[2]),
		stripContentPx,
		`the documented "strip" term (${commentMatch[2]}px) must equal the strip's real derived ` +
			`LESS content height (${stripContentPx}px)`
	);
	assert.equal(
		Number(commentMatch[4]),
		mixerChromePx,
		'the documented "chrome" term must equal the real mixer chrome'
	);
});

// Sol P1 finding (comment 3963232872, BLOCKING): MORE mode's strip never
// collapses (30px EQs, FILTER present) and pin 246b0f5's `flex-shrink: 0`
// rule means it must fit inside the shared deck-area floor too, exactly
// like LESS - this was never checked before that finding.
test('the mixer MORE floor (+page.svelte) covers the real ChannelStrip MORE-mode content height', () => {
	const { stripSrc, knobBoxPx, eqGapPx, faderMinPx, stripFlexGapPx, stripPaddingPx } = readSharedStripNumbers();
	const pageSrc = readFileSync(PAGE_PATH, 'utf8');
	const mixerChromePx = readMixerChromePx();

	const trimSizeMatch = firstMatch(stripSrc, /const TRIM_SIZE = (\d+(?:\.\d+)?);/, 'TRIM_SIZE');
	const eqSizeMatch = firstMatch(stripSrc, /const EQ_SIZE = (\d+(?:\.\d+)?);/, 'EQ_SIZE');
	const filterSizeMatch = firstMatch(stripSrc, /const FILTER_SLOT_SIZE = (\d+(?:\.\d+)?);/, 'FILTER_SLOT_SIZE');
	const trimSize = Number(trimSizeMatch[1]);
	const eqSize = Number(eqSizeMatch[1]);
	const filterSize = Number(filterSizeMatch[1]);

	// The base (non-`.strip.less`) rules are MORE's numbers - `.strip.less`
	// only ever overrides them for LESS.
	function baseMarginPx(selector, label, side) {
		const rule = firstMatch(
			stripSrc,
			new RegExp(`^\\t\\.${selector}\\s*\\{([^}]*)\\}`, 'm'),
			`a .${selector} rule`
		);
		const marginMatch = firstMatch(rule[1], new RegExp(`margin-${side}:\\s*(\\d+)px`), `${label}'s MORE margin`);
		return Number(marginMatch[1]);
	}

	const trimMarginPx = baseMarginPx('trim-slot', 'trim-slot', 'bottom');
	const cueMarginPx = baseMarginPx('cue-btn', 'cue-btn', 'bottom');
	const faderMarginPx = baseMarginPx('fader-slot', 'fader-slot', 'bottom');
	const stemLabelMarginPx = baseMarginPx('stem-label', 'stem-label', 'top');

	const filterSlotRule = firstMatch(stripSrc, /^\t\.filter-slot\s*\{([^}]*)\}/m, 'a .filter-slot rule');
	const filterMarginTopMatch = firstMatch(filterSlotRule[1], /margin-top:\s*(\d+)px/, "filter-slot's margin-top");
	const filterMarginBottomMatch = firstMatch(
		filterSlotRule[1],
		/margin-bottom:\s*(\d+)px/,
		"filter-slot's margin-bottom"
	);
	const filterMarginPx = Number(filterMarginTopMatch[1]) + Number(filterMarginBottomMatch[1]);

	// 8 children in MORE (FILTER present): ch-num, trim-slot, eq-stack,
	// filter-slot, cue-btn, fader-slot, stem-label, stem-slot -> 7 flex gaps.
	const CHILD_COUNT_MORE = 8;
	const flexGapsTotalPx = (CHILD_COUNT_MORE - 1) * stripFlexGapPx;

	const stripContentPx =
		Math.max(CH_NUM_PX, CAL_CONTROLS_PX) +
		(knobBoxPx(trimSize) + trimMarginPx) +
		(3 * knobBoxPx(eqSize) + 2 * eqGapPx) +
		(knobBoxPx(filterSize) + filterMarginPx) +
		(CUE_BTN_PX + cueMarginPx) +
		(faderMinPx + faderMarginPx) +
		(STEM_LABEL_PX + stemLabelMarginPx) +
		STEM_SLOT_PX +
		flexGapsTotalPx +
		stripPaddingPx;

	const requiredMixerPx = TOGGLE_PX + stripContentPx + LOWER_PX + mixerChromePx;

	const moreFloorPx = readDeckAreaFloorPx(pageSrc, 'MORE');

	// Unlike LESS, MORE's real requirement is not a whole number
	// (FILTER_SLOT_SIZE = 35.1px), so the floor is a rounded-up whole px and
	// this only asserts coverage (>=), not an exact documented-comment match.
	assert.ok(
		moreFloorPx >= requiredMixerPx,
		`+page.svelte's MORE deck-area floor (${moreFloorPx}px) must be >= the mixer's real MORE-mode ` +
			`content requirement (${requiredMixerPx}px = toggle ${TOGGLE_PX} + strip ${stripContentPx} ` +
			`+ lower ${LOWER_PX} + chrome ${mixerChromePx}), or the EQ/FILTER/fader/level-meter/STEM ` +
			`controls clip again`
	);
});

test('the MORE/LESS toggle is compact (pin 246b0f5: "toggle is too big ... pushing EQs down")', () => {
	const src = readFileSync(MIXER_PATH, 'utf8');
	const toggleRule = firstMatch(src, /\.deck-layout-toggle\s*\{([^}]*)\}/, 'a .deck-layout-toggle rule');
	const paddingMatch = firstMatch(toggleRule[1], /padding-bottom:\s*(\d+)px/, 'toggle padding-bottom');
	assert.ok(Number(paddingMatch[1]) <= 2, 'toggle padding-bottom must be shrunk to <= 2px');

	const btnRule = firstMatch(src, /\.deck-layout-btn\s*\{([^}]*)\}/, 'a .deck-layout-btn rule');
	const fontMatch = firstMatch(btnRule[1], /font-size:\s*(\d+)px/, 'toggle button font-size');
	assert.ok(Number(fontMatch[1]) <= 9, 'toggle button font-size must be shrunk to <= 9px');
});
