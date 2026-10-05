import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

import { compile } from 'svelte/compiler';

const EQ_DEFAULT_SIZE = 30;
const EXPECTED_TRIM_SIZE = 39;
const EXPECTED_FILTER_SIZE = 21;

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
test('pin 4eebbc65a699 rainbow stops stay on the non-blue arc and use ping-pong animation', async () => {
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
		/\bsize\s*=\s*30\s*,[^}]*\}:\s*Props\s*=\s*\$props\(\)/,
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

test('MIXUX-03 ChannelStrip TRIM is 30% larger than EQ dials in MORE mode', async () => {
	const src = await readFile('src/lib/components/rb/mixer/ChannelStrip.svelte', 'utf8');

	const constMatch = src.match(/const TRIM_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(constMatch, 'expected a named TRIM_SIZE constant, not a magic number on the Knob line');
	assert.equal(Number(constMatch[1]), EXPECTED_TRIM_SIZE);
	assert.equal(EXPECTED_TRIM_SIZE, EQ_DEFAULT_SIZE * 1.3);

	const knobTag = (label) => new RegExp(`<Knob\\b(?:(?!/>)[\\s\\S])*?label="${label}"(?:(?!/>)[\\s\\S])*?/>`);

	const trimKnobLine = src.match(knobTag('TRIM'));
	assert.ok(trimKnobLine, 'TRIM Knob element not found');
	assert.match(trimKnobLine[0], /size=\{trimSize\}/);

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

	const dialKnobTag = (dial) =>
		new RegExp(`<Knob\\b(?:(?!/>)[\\s\\S])*?label=\\{${dial}\\.label\\}(?:(?!/>)[\\s\\S])*?/>`);
	for (const [label, dial, band] of [
		['HI', 'hiDial', 'high'],
		['MID', 'midDial', 'mid'],
		['LOW', 'lowDial', 'low']
	]) {
		assert.match(
			src,
			new RegExp(`const ${dial} = \\$derived\\.by\\(\\(\\) => dialView\\('${band}', eq\\w+, '${label}',`),
			`${dial} must be the ${label} EQ band dial`
		);
		const knobLine = src.match(dialKnobTag(dial));
		assert.ok(knobLine, `${label} Knob element not found`);
		assert.match(knobLine[0], /size=\{eqSize\}/, `${label} must pass size={eqSize} (pin 246b0f5) so LESS mode can shrink it`);
	}
	const eqSizeDerived = src.match(/const eqSize = \$derived\(less \? LESS_EQ_SIZE : EQ_SIZE\);/);
	assert.ok(eqSizeDerived, 'eqSize must fall back to EQ_SIZE whenever less is false');
	const eqSizeConstMatch = src.match(/const EQ_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(eqSizeConstMatch, 'expected a named EQ_SIZE constant spelling out Knob\'s own default');
	assert.equal(Number(eqSizeConstMatch[1]), 30);
	const lessEqSizeMatch = src.match(/const LESS_EQ_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(lessEqSizeMatch, 'expected a named LESS_EQ_SIZE constant for pin 246b0f5 LESS mode');
	assert.ok(Number(lessEqSizeMatch[1]) < 30, 'LESS_EQ_SIZE must actually be smaller than the 30px EQ default');
});

test('MIXUX-03 ChannelStrip FILTER slot is 30% smaller than EQ dials and wired live', async () => {
	const src = await readFile('src/lib/components/rb/mixer/ChannelStrip.svelte', 'utf8');
	const constMatch = src.match(/const FILTER_SLOT_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(constMatch, 'FILTER slot needs a named size rather than a magic number');
	assert.equal(Number(constMatch[1]), EXPECTED_FILTER_SIZE);
	assert.equal(EXPECTED_FILTER_SIZE, EQ_DEFAULT_SIZE * 0.7);

	const knobTag = (label) => new RegExp(`<Knob\\b(?:(?!/>)[\\s\\S])*?label="${label}"(?:(?!/>)[\\s\\S])*?/>`);
	const filterKnob = src.match(knobTag('FILTER'));
	assert.ok(filterKnob, 'FILTER Knob element not found');
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
		`the LESS FILTER dial must be smaller than MORE's ${EXPECTED_FILTER_SIZE}px`
	);
	assert.match(filterKnob[0], /value=\{filter\}/, 'FILTER must read the strip\'s filter prop');
	assert.match(filterKnob[0], /onchange=\{onfilter\}/, 'FILTER must emit changes');
	assert.doesNotMatch(filterKnob[0], /\binert\b/, 'FILTER must not be an inert stub');
});
