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
// SINGLE row - so `<Mixer />` shares +page.svelte's LESS deck-area floor
// with the deck columns, even though pin 862cd3 never collapses decks 1/2's
// mixer channel strips (only strips 3/4's WIDTH goes to 0 - Mixer.svelte's
// `.strips.less` rule). Before this pin that floor was sized only for one
// deck panel (248px); the mixer's own LESS-mode content needs more than
// that, and `.rb-mixer { overflow: hidden }` silently clipped the excess -
// the fader and its ChannelLevelMeter, being the LAST elements in the
// flex column, were the first casualty. This file derives the mixer's real
// LESS-mode height requirement from ChannelStrip.svelte/Mixer.svelte's own
// source (the same style library-min-5-rows.test.mjs uses for the deck/
// library floors) and asserts +page.svelte's floor actually covers it,
// rather than pinning a remembered number. The render-level proof (a real
// browser, not just source text) is performance-less-mode-mixer-levels.spec.ts.
//
// Four numbers below (TOGGLE_PX, CH_NUM_PX, CUE_BTN_PX, STEM_SLOT_PX) are
// measured constants, not regex-derived: they are rendered heights driven by
// browser font-metric line-height, which no static CSS regex can predict
// exactly (the same reason +page.svelte's "one deck needs 248px" and
// "library needs 272px" floors are themselves documented, measured
// arithmetic rather than fully re-derived from font metrics). They are
// unchanged by this pin (STEM/CUE/ch-num are not touched by the `less`
// prop), measured Playwright/Chromium, 1280x800, LESS mode - see
// performance-less-mode-mixer-levels.spec.ts for the live equivalent.
const TOGGLE_PX = 17;
const CH_NUM_PX = 10;
const CUE_BTN_PX = 14;
const STEM_SLOT_PX = 54;
const LOWER_PX = 76;

const CHANNEL_STRIP_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/mixer/ChannelStrip.svelte', import.meta.url)
);
const KNOB_PATH = fileURLToPath(new URL('../../src/lib/components/rb/mixer/Knob.svelte', import.meta.url));
const MIXER_PATH = fileURLToPath(new URL('../../src/lib/components/rb/Mixer.svelte', import.meta.url));
const PAGE_PATH = fileURLToPath(new URL('../../src/routes/performance/+page.svelte', import.meta.url));

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

test('the mixer LESS floor (+page.svelte) covers the real ChannelStrip LESS-mode content height', () => {
	const stripSrc = readFileSync(CHANNEL_STRIP_PATH, 'utf8');
	const knobSrc = readFileSync(KNOB_PATH, 'utf8');
	const mixerSrc = readFileSync(MIXER_PATH, 'utf8');
	const pageSrc = readFileSync(PAGE_PATH, 'utf8');

	// Knob's own caption formula (shared by every dial size): dial diameter
	// (the `size` prop) + a 1px caption gap + 8px caption text.
	const gapMatch = firstMatch(knobSrc, /--knob-caption-gap: (\d+)px/, "Knob's caption gap");
	const captionMatch = firstMatch(knobSrc, /--knob-caption-size: (\d+)px/, "Knob's caption size");
	const captionExtraPx = Number(gapMatch[1]) + Number(captionMatch[1]);

	function knobBoxPx(size) {
		return size + captionExtraPx;
	}

	const lessTrimMatch = firstMatch(stripSrc, /const LESS_TRIM_SIZE = (\d+(?:\.\d+)?);/, 'LESS_TRIM_SIZE');
	const lessEqMatch = firstMatch(stripSrc, /const LESS_EQ_SIZE = (\d+(?:\.\d+)?);/, 'LESS_EQ_SIZE');
	const lessTrimSize = Number(lessTrimMatch[1]);
	const lessEqSize = Number(lessEqMatch[1]);

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

	// STEM's own label height is a measured constant too (font-metric, see
	// the module doc comment) - 8px, unchanged by this pin.
	const STEM_LABEL_PX = 8;

	// 7 children survive in LESS (FILTER drops out): ch-num, trim-slot,
	// eq-stack, cue-btn, fader-slot, stem-label, stem-slot -> 6 flex gaps.
	const CHILD_COUNT_LESS = 7;
	const flexGapsTotalPx = (CHILD_COUNT_LESS - 1) * stripFlexGapPx;

	const stripContentPx =
		CH_NUM_PX +
		(knobBoxPx(lessTrimSize) + trimMarginPx) +
		(3 * knobBoxPx(lessEqSize) + 2 * eqGapPx) +
		(CUE_BTN_PX + cueMarginPx) +
		(faderMinPx + faderMarginPx) +
		(STEM_LABEL_PX + stemLabelMarginPx) +
		STEM_SLOT_PX +
		flexGapsTotalPx +
		stripPaddingPx;

	const requiredMixerPx = TOGGLE_PX + stripContentPx + LOWER_PX;

	const lessFloorMatch = firstMatch(
		pageSrc,
		/\.perf-root\.deck-layout-less\s*\{[\s\S]*?minmax\(\s*(\d+)px,/,
		'the LESS deck-area minmax floor'
	);
	const lessFloorPx = Number(lessFloorMatch[1]);

	assert.ok(
		lessFloorPx >= requiredMixerPx,
		`+page.svelte's LESS deck-area floor (${lessFloorPx}px) must be >= the mixer's real LESS-mode ` +
			`content requirement (${requiredMixerPx}px = toggle ${TOGGLE_PX} + strip ${stripContentPx} ` +
			`+ lower ${LOWER_PX}), or the fader/level-meter/EQ/STEM controls clip again`
	);

	// The documented breakdown comment in +page.svelte must not silently
	// drift from what the CSS actually enforces.
	const commentMatch = firstMatch(
		pageSrc,
		/toggle (\d+) \+ strip (\d+) \+ lower (\d+) = (\d+)px/,
		'the documented toggle+strip+lower=N breakdown comment'
	);
	assert.equal(Number(commentMatch[4]), Number(commentMatch[1]) + Number(commentMatch[2]) + Number(commentMatch[3]));
	assert.equal(
		Number(commentMatch[4]),
		lessFloorPx,
		'the documented breakdown total must equal the actual CSS floor, or the comment has drifted'
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
