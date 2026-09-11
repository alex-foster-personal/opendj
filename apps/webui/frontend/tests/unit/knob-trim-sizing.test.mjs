import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

import { compile } from 'svelte/compiler';

// Pin 8cabf5b1df1e: TRIM now sits halfway between its previous 21px and the
// 30px EQ dials; FILTER is 10% smaller than its former 39px presentation.
// Knob.svelte renders every dial at one fixed SIZE, so TRIM cannot differ
// from HI/MID/LOW today. A `size` prop lets a caller scale one instance; the
// viewBox stays fixed at "0 0 30 30" so width/height scale the whole ring,
// cap and indicator geometry together. The caption-inclusive wrapper is the
// hit target, while its caption stays ordinary 8px text at every dial size.
//
// Regression lines:
// - if Knob.svelte hardcodes svg width/height to a literal instead of the
//   `size` prop then no per-instance sizing is possible at all
// - if ChannelStrip's TRIM knob omits a `size` prop then TRIM renders at the
//   same 30px default as HI/MID/LOW, i.e. the whole point of MIXUX-03
// - if an EQ knob (HI/MID/LOW) is ever given a `size` prop then EQ dials stop
//   being the fixed reference the requirement is measured against
// - if a resized dial scales its caption then FILTER text becomes larger and
//   less crisp than the EQ labels

const EQ_DEFAULT_SIZE = 30;
const EXPECTED_TRIM_SIZE = (21 + EQ_DEFAULT_SIZE) / 2;
const EXPECTED_FILTER_SIZE = 39 * 0.9;

// Pin 8cabf5b1df1e:
// [if] a knob is resized [then] its dial and pointer target resize but its caption remains legible 8px text [else ⛔️]
test('Knob compiles with one caption-inclusive interactive wrapper whose text stays unscaled', async () => {
	const src = await readFile('src/lib/components/rb/mixer/Knob.svelte', 'utf8');
	assert.doesNotThrow(() => compile(src, { filename: 'Knob.svelte', generate: 'server' }));
	assert.match(src, /--knob-caption-size: 8px/);
	assert.match(src, /--knob-caption-gap: 1px/);
	assert.doesNotMatch(src, /const scale = size \/ KNOB_BASE_SIZE/);
	assert.doesNotMatch(src, /--knob-caption-size: \$\{8 \* scale\}px/);
	assert.match(src, /width: max-content;/);
	assert.match(src, /min-width: var\(--knob-dial-size\);/);
	assert.match(src, /<div[\s\S]*?role="slider"[\s\S]*?onpointerdown=\{handlePointerDown\}/);
});

// Pin 4eebbc65a699 / #939:
// [if] the rainbow indicator animates [then] every authored stop is non-blue and reverses instead of wrapping [else ⛔️]
test('rainbow stops stay on the non-blue arc and use a ping-pong animation direction', async () => {
	const src = await readFile('src/lib/components/rb/mixer/Knob.svelte', 'utf8');
	assert.match(src, /RAINBOW_STOPS = \['#e23a32', '#e8a13a', '#d7d83a', '#35c04f'\]/);
	assert.match(
		src,
		/animation:\s*knob-rainbow\s+4\.5s\s+ease-in-out\s+infinite\s+alternate/,
		'ping-pong direction removes the missing-blue wrap seam'
	);
	assert.doesNotMatch(src, /#(?:2f6fd6|7b5cff)/i, 'the former blue/indigo rainbow stops must not return');
});

test('Knob.svelte exposes a size prop that drives the visual SVG and makes the whole wrapper the interactive target', async () => {
	const src = await readFile('src/lib/components/rb/mixer/Knob.svelte', 'utf8');
	assert.match(src, /size\?:\s*number/, 'Props interface should declare an optional size prop');
	assert.match(
		src,
		/size\s*=\s*30\s*\}:\s*Props\s*=\s*\$props\(\)/,
		'size should default to the historic 30px dial so every other caller is unaffected'
	);
	assert.match(src, /<svg[^>]*\bwidth=\{size\}/, 'svg width must be driven by the size prop');
	assert.match(src, /<svg[^>]*\bheight=\{size\}/, 'svg height must be driven by the size prop');
	assert.match(src, /<div[\s\S]*?role="slider"[\s\S]*?onpointerdown=\{handlePointerDown\}/, 'the wrapper, including the caption, must receive pointer input');
	assert.match(
		src,
		/viewBox="0 0 30 30"/,
		'viewBox stays fixed so width/height scale the whole ring/cap/indicator uniformly'
	);
});

test('ChannelStrip places TRIM halfway between its old size and the EQ dials', async () => {
	const src = await readFile('src/lib/components/rb/mixer/ChannelStrip.svelte', 'utf8');

	const constMatch = src.match(/const TRIM_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(constMatch, 'expected a named TRIM_SIZE constant, not a magic number on the Knob line');
	assert.equal(
		Number(constMatch[1]),
		EXPECTED_TRIM_SIZE,
		'TRIM diameter must be halfway between its old 21px size and the 30px EQ dial - pin 246b0f5 ' +
			'LESS mode uses a separate, smaller LESS_TRIM_SIZE (asserted below), so this MORE-mode ' +
			'constant and value must stay exactly as MIXUX-03 shipped it'
	);

	// Each knob's own attribute span: content up to '/>' must never cross a
	// '/>' itself, or the match bleeds into a sibling <Knob ... /> tag - an
	// onchange={(v) => ...} arrow function attribute contains a literal '>',
	// which is why this can't just be [^>]*.
	const knobTag = (label) => new RegExp(`<Knob\\b(?:(?!/>)[\\s\\S])*?label="${label}"(?:(?!/>)[\\s\\S])*?/>`);

	// Pin 246b0f5: LESS mode has to shrink TRIM/EQ to fit the shrunk deck-area
	// row (see +page.svelte and CHANNEL_STRIP-LESS-FLOOR.test.mjs), so both
	// now pass a `less`-derived variable instead of the bare MORE constant.
	// The MORE-mode guarantee above (TRIM stays exactly halfway between 21
	// and the 30px EQ dial, EQ dials stay the fixed default) is preserved
	// BEHAVIORALLY, not textually: both derived variables fall back to the
	// unchanged MORE constants (TRIM_SIZE, Knob's own 30px default) whenever
	// `less` is false - asserted below by reading the `$derived(...)`
	// definitions themselves, not just their names.
	const trimKnobLine = src.match(knobTag('TRIM'));
	assert.ok(trimKnobLine, 'TRIM Knob element not found');
	assert.match(trimKnobLine[0], /size=\{trimSize\}/, 'TRIM Knob must pass size={trimSize}');

	const trimSizeDerived = src.match(/const trimSize = \$derived\(less \? LESS_TRIM_SIZE : TRIM_SIZE\);/);
	assert.ok(
		trimSizeDerived,
		'trimSize must fall back to the unchanged MORE-mode TRIM_SIZE whenever less is false'
	);
	const lessTrimSizeMatch = src.match(/const LESS_TRIM_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(lessTrimSizeMatch, 'expected a named LESS_TRIM_SIZE constant for pin 246b0f5 LESS mode');
	assert.ok(
		Number(lessTrimSizeMatch[1]) < EXPECTED_TRIM_SIZE,
		'LESS_TRIM_SIZE must actually be smaller than MORE mode TRIM_SIZE, or LESS saves no height'
	);

	for (const label of ['HI', 'MID', 'LOW']) {
		const knobLine = src.match(knobTag(label));
		assert.ok(knobLine, `${label} Knob element not found`);
		assert.match(
			knobLine[0],
			/size=\{eqSize\}/,
			`${label} must pass size={eqSize} (pin 246b0f5) so LESS mode can shrink it`
		);
	}
	// Was `$derived(less ? LESS_EQ_SIZE : undefined)` (falling through to
	// Knob's own `size = 30` default) until Sol's CI type-check finding: with
	// `exactOptionalPropertyTypes: true`, an explicit `size={undefined}` is
	// not assignable to Knob's `size?: number` Props (undefined-the-value is
	// distinct from the prop being absent) - see ChannelStrip.svelte's
	// EQ_SIZE comment. EQ_SIZE is spelled out as the same 30px Knob already
	// defaulted to, so this is a type-correctness fix, not a behavior change
	// - asserted below by requiring EQ_SIZE to actually equal 30.
	const eqSizeDerived = src.match(/const eqSize = \$derived\(less \? LESS_EQ_SIZE : EQ_SIZE\);/);
	assert.ok(
		eqSizeDerived,
		'eqSize must fall back to EQ_SIZE whenever less is false, so HI/MID/LOW render at the ' +
			"unchanged Knob default (30px) - the fixed reference EQ dial MIXUX-03's MORE-mode behavior depends on"
	);
	const eqSizeConstMatch = src.match(/const EQ_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(eqSizeConstMatch, 'expected a named EQ_SIZE constant spelling out Knob\'s own default');
	assert.equal(
		Number(eqSizeConstMatch[1]),
		30,
		'EQ_SIZE must equal Knob.svelte\'s own default size (30), or MORE mode\'s EQ dials silently resize'
	);
	const lessEqSizeMatch = src.match(/const LESS_EQ_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(lessEqSizeMatch, 'expected a named LESS_EQ_SIZE constant for pin 246b0f5 LESS mode');
	assert.ok(
		Number(lessEqSizeMatch[1]) < 30,
		'LESS_EQ_SIZE must actually be smaller than the 30px EQ default, or LESS saves no height'
	);
});

// Pin 8cabf5b1df1e, updated on the #1021 merge:
// [if] the strip renders its FILTER slot [then] it is 10% smaller than its 39px predecessor AND it is the live dial issue #990 wired, not the inert stub [else ⛔️]
//
// main's version of this test asserted `inert` with the message "this branch
// must not claim live FILTER DSP from PR #1021/#492". #1021 IS that live DSP,
// so merging it is the event that guard was holding the slot for: the size
// assertion (the part that is really about layout) is unchanged, and the
// ownership assertion flips from "still a stub" to "wired, and wired to this
// strip's own props" so it can still fail if the dial is ever cut back to a
// decoration.
test('ChannelStrip keeps the reduced FILTER slot size and renders it as a live dial', async () => {
	const src = await readFile('src/lib/components/rb/mixer/ChannelStrip.svelte', 'utf8');
	const constMatch = src.match(/const FILTER_SLOT_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(constMatch, 'FILTER slot needs a named size rather than a magic number');
	assert.equal(Number(constMatch[1]), EXPECTED_FILTER_SIZE);

	const knobTag = (label) => new RegExp(`<Knob\\b(?:(?!/>)[\\s\\S])*?label="${label}"(?:(?!/>)[\\s\\S])*?/>`);
	const filterKnob = src.match(knobTag('FILTER'));
	assert.ok(filterKnob, 'FILTER Knob element not found');
	// Pin 2917b0eca218: FILTER is no longer unmounted in LESS, it is shrunk
	// like TRIM and the EQs, so the Knob takes the derived `filterSize` and
	// the MORE-mode constant is asserted through that derivation instead of
	// on the tag. Both halves are checked, so a `filterSize` that stopped
	// depending on FILTER_SLOT_SIZE would still be caught.
	assert.match(filterKnob[0], /size=\{filterSize\}/);
	assert.match(
		src,
		/const filterSize = \$derived\(less \? LESS_FILTER_SIZE : FILTER_SLOT_SIZE\);/,
		'filterSize must resolve to FILTER_SLOT_SIZE in MORE and the shrunk size in LESS'
	);
	const lessFilterMatch = src.match(/const LESS_FILTER_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(lessFilterMatch, 'LESS needs its own named FILTER size');
	assert.ok(
		Number(lessFilterMatch[1]) < EXPECTED_FILTER_SIZE,
		`the LESS FILTER dial (${lessFilterMatch[1]}px) must be smaller than MORE's ` +
			`${EXPECTED_FILTER_SIZE}px, or LESS is not compacting anything`
	);
	assert.match(filterKnob[0], /value=\{filter\}/, 'FILTER must read the strip\'s filter prop, not a frozen 0.5');
	assert.match(filterKnob[0], /onchange=\{onfilter\}/, 'FILTER must emit changes, not sit inert');
	assert.doesNotMatch(filterKnob[0], /\binert\b/, 'the inert stub is superseded by issue #990\'s live dial');
});
