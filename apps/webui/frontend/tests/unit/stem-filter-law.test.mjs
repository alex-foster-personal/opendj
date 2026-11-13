import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let law;
let guards;

before(async () => {
	law = await loadTypeScriptModule('src/lib/rb/stem-filter-law.ts');
	guards = await loadTypeScriptModule('src/lib/rb/audio-engine-guards.ts');
});

test('neutral knob bypasses the stem filter', () => {
	// [if] the stem filter knob sits at 0.5 or inside the dead zone [then] the part is allpass and bypassed, [else stop].
	for (const v of [0.5, 0.501, 0.499]) {
		const out = law.stemFilterBiquad(v);
		assert.equal(out.type, 'allpass');
		assert.equal(out.bypassed, true);
	}
});

test('low and high knob follow the channel FILTER law', () => {
	// [if] the knob is 0.2 or 0.8 [then] lowpass or highpass at the channel law frequency, [else stop].
	const lo = law.stemFilterBiquad(0.2);
	assert.deepEqual(lo, {
		type: 'lowpass',
		frequency: guards.filterParamsFromKnob(0.2).lpHz,
		bypassed: false
	});
	const hi = law.stemFilterBiquad(0.8);
	assert.deepEqual(hi, {
		type: 'highpass',
		frequency: guards.filterParamsFromKnob(0.8).hpHz,
		bypassed: false
	});
});

test('bad input is neutral or clamped', () => {
	// [if] the knob is NaN or outside 0..1 [then] NaN is neutral and the rest clamp to the ends, [else stop].
	assert.equal(law.stemFilterBiquad(Number.NaN).bypassed, true);
	assert.equal(law.normaliseStemFilterValue(Infinity), 0.5);
	assert.deepEqual(law.stemFilterBiquad(-3), law.stemFilterBiquad(0));
	assert.deepEqual(law.stemFilterBiquad(7), law.stemFilterBiquad(1));
});
