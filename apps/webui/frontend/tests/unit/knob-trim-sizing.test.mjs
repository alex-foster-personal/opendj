import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

// MIXUX-03: "Trim dial should be 30% smaller circumference than EQ dials."
// Knob.svelte renders every dial at one fixed SIZE, so TRIM cannot differ
// from HI/MID/LOW today. A `size` prop lets a caller scale one instance; the
// viewBox stays fixed at "0 0 30 30" so width/height scale the whole ring,
// cap and indicator geometry together - the svg IS the hit area, so a
// resized knob's hit area scales with it and there is no dead zone.
//
// Regression lines:
// - if Knob.svelte hardcodes svg width/height to a literal instead of the
//   `size` prop then no per-instance sizing is possible at all
// - if ChannelStrip's TRIM knob omits a `size` prop then TRIM renders at the
//   same 30px default as HI/MID/LOW, i.e. the whole point of MIXUX-03
// - if an EQ knob (HI/MID/LOW) is ever given a `size` prop then EQ dials stop
//   being the fixed reference the requirement is measured against
// - if TRIM_SIZE drifts off 70% of the EQ default then the ratio is not
//   actually "30% smaller circumference"

const EQ_DEFAULT_SIZE = 30;
const EXPECTED_TRIM_SIZE = EQ_DEFAULT_SIZE * 0.7; // 30% smaller circumference

test('Knob.svelte exposes a size prop that drives the SVG hit area, viewBox fixed', async () => {
	const src = await readFile('src/lib/components/rb/mixer/Knob.svelte', 'utf8');
	assert.match(src, /size\?:\s*number/, 'Props interface should declare an optional size prop');
	assert.match(
		src,
		/size\s*=\s*30\s*\}:\s*Props\s*=\s*\$props\(\)/,
		'size should default to the historic 30px dial so every other caller is unaffected'
	);
	assert.match(src, /<svg[^>]*\bwidth=\{size\}/, 'svg width must be driven by the size prop');
	assert.match(src, /<svg[^>]*\bheight=\{size\}/, 'svg height must be driven by the size prop');
	assert.match(
		src,
		/viewBox="0 0 30 30"/,
		'viewBox stays fixed so width/height scale the whole ring/cap/indicator uniformly'
	);
});

test('ChannelStrip TRIM dial is 70% of the EQ dial size (MIXUX-03: 30% smaller circumference)', async () => {
	const src = await readFile('src/lib/components/rb/mixer/ChannelStrip.svelte', 'utf8');

	const constMatch = src.match(/const TRIM_SIZE = (\d+(?:\.\d+)?);/);
	assert.ok(constMatch, 'expected a named TRIM_SIZE constant, not a magic number on the Knob line');
	assert.equal(
		Number(constMatch[1]),
		EXPECTED_TRIM_SIZE,
		'TRIM diameter must be exactly 70% of the 30px EQ dial diameter'
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
