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
		'TRIM diameter must be halfway between its old 21px size and the 30px EQ dial'
	);

	// Each knob's own attribute span: content up to '/>' must never cross a
	// '/>' itself, or the match bleeds into a sibling <Knob ... /> tag - an
	// onchange={(v) => ...} arrow function attribute contains a literal '>',
	// which is why this can't just be [^>]*.
	const knobTag = (label) => new RegExp(`<Knob\\b(?:(?!/>)[\\s\\S])*?label="${label}"(?:(?!/>)[\\s\\S])*?/>`);

	const trimKnobLine = src.match(knobTag('TRIM'));
	assert.ok(trimKnobLine, 'TRIM Knob element not found');
	assert.match(trimKnobLine[0], /size=\{TRIM_SIZE\}/, 'TRIM Knob must pass size={TRIM_SIZE}');

	for (const label of ['HI', 'MID', 'LOW']) {
		const knobLine = src.match(knobTag(label));
		assert.ok(knobLine, `${label} Knob element not found`);
		assert.doesNotMatch(
			knobLine[0],
			/size=\{/,
			`${label} must stay at the Knob default size - it is the fixed reference EQ dial`
		);
	}
});

// Pin 8cabf5b1df1e:
// [if] the strip renders its main-owned inert FILTER slot [then] it is 10% smaller than its 39px predecessor without claiming #492's Color-FX ownership [else ⛔️]
test('ChannelStrip reduces only the current-main inert FILTER slot and leaves Color-FX ownership explicit', async () => {
	const src = await readFile('src/lib/components/rb/mixer/ChannelStrip.svelte', 'utf8');
	const constMatch = src.match(/const FILTER_SLOT_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(constMatch, 'FILTER slot needs a named size rather than a magic number');
	assert.equal(Number(constMatch[1]), EXPECTED_FILTER_SIZE);

	const knobTag = (label) => new RegExp(`<Knob\\b(?:(?!/>)[\\s\\S])*?label="${label}"(?:(?!/>)[\\s\\S])*?/>`);
	const filterKnob = src.match(knobTag('FILTER'));
	assert.ok(filterKnob, 'current-main FILTER Knob element not found');
	assert.match(filterKnob[0], /size=\{FILTER_SLOT_SIZE\}/);
	assert.match(filterKnob[0], /inert/, 'this branch must not claim live FILTER DSP from PR #1021/#492');
	assert.match(
		src,
		/Current main owns this inert FILTER slot's presentation only; PR #492 owns the live COLOR-FX replacement/,
		'future slot ownership must stay explicit so the #492 merge can carry the size safely'
	);
});
