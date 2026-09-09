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

test('ChannelStrip.svelte drops FILTER and shrinks TRIM/EQ only in LESS mode', () => {
	const src = readFileSync(CHANNEL_STRIP_PATH, 'utf8');
	assert.match(
		src,
		/\{#if !less\}\s*<div class="filter-slot">/,
		'FILTER must be conditionally unmounted in LESS mode (it is an inert stub, not a required dial)'
	);
	assert.match(src, /size=\{trimSize\}/, 'TRIM must be driven by the derived trimSize');
	assert.match(src, /size=\{eqSize\}/, 'HI/MID/LOW must be driven by the derived eqSize');
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

	const eqGapMatch = firstMatch(stripSrc, /\.eq-stack\s*\{[^}]*gap:\s*(\d+)px/, '.eq-stack gap');
	const eqGapPx = Number(eqGapMatch[1]);

	const faderMinMatch = firstMatch(
		stripSrc,
		/\.fader-slot\s*\{[^}]*min-height:\s*(\d+)px/,
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

	// 7 children survive in LESS (FILTER drops out): ch-num, trim-slot,
	// eq-stack, cue-btn, fader-slot, stem-label, stem-slot -> 6 flex gaps.
	const CHILD_COUNT_LESS = 7;
	const flexGapsTotalPx = (CHILD_COUNT_LESS - 1) * stripFlexGapPx;

	const stripContentPx =
		Math.max(CH_NUM_PX, CAL_CONTROLS_PX) +
		(knobBoxPx(lessTrimSize) + trimMarginPx) +
		(3 * knobBoxPx(lessEqSize) + 2 * eqGapPx) +
		(CUE_BTN_PX + cueMarginPx) +
		(faderMinPx + faderMarginPx) +
		(STEM_LABEL_PX + stemLabelMarginPx) +
		STEM_SLOT_PX +
		flexGapsTotalPx +
		stripPaddingPx;

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
		const rule = firstMatch(stripSrc, new RegExp(`\\.${selector}\\s*\\{([^}]*)\\}`), `a .${selector} rule`);
		const marginMatch = firstMatch(rule[1], new RegExp(`margin-${side}:\\s*(\\d+)px`), `${label}'s MORE margin`);
		return Number(marginMatch[1]);
	}

	const trimMarginPx = baseMarginPx('trim-slot', 'trim-slot', 'bottom');
	const cueMarginPx = baseMarginPx('cue-btn', 'cue-btn', 'bottom');
	const faderMarginPx = baseMarginPx('fader-slot', 'fader-slot', 'bottom');
	const stemLabelMarginPx = baseMarginPx('stem-label', 'stem-label', 'top');

	const filterSlotRule = firstMatch(stripSrc, /\.filter-slot\s*\{([^}]*)\}/, 'a .filter-slot rule');
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
