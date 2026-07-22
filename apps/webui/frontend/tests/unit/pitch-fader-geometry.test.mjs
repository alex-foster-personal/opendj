import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let faderValueFromPitchRatio;
let pitchRatioFromFaderValue;
let MIN_TEMPO_RATIO;

before(async () => {
	({ faderValueFromPitchRatio, pitchRatioFromFaderValue, MIN_TEMPO_RATIO } = await loadTypeScriptModule(
		'src/lib/components/rb/deck/pitch-fader-geometry.ts'
	));
});

test('center fader value is always 0% pitch regardless of the selected range', () => {
	for (const pitchRangePct of [8, 16, 100]) {
		assert.equal(pitchRatioFromFaderValue(0.5, pitchRangePct), 1);
	}
});

test('fader travel spans exactly the selected pitch range', () => {
	assert.equal(pitchRatioFromFaderValue(1, 8), 1.08);
	assert.equal(pitchRatioFromFaderValue(0, 8), 0.92);
	assert.equal(pitchRatioFromFaderValue(1, 16), 1.16);
	assert.equal(pitchRatioFromFaderValue(0, 16), 0.84);
	assert.equal(pitchRatioFromFaderValue(1, 100), 2);
	assert.equal(pitchRatioFromFaderValue(0, 100), MIN_TEMPO_RATIO);
	assert.ok(pitchRatioFromFaderValue(0, 100) > 0);
});

test('out-of-travel fader values clamp to the range endpoints', () => {
	assert.equal(pitchRatioFromFaderValue(1.4, 8), 1.08);
	assert.equal(pitchRatioFromFaderValue(-0.4, 8), 0.92);
});

test('ratio -> fader-value -> ratio round-trips for values within the selected range', () => {
	for (const pitchRangePct of [8, 16, 100]) {
		for (const ratio of [1, 1 - pitchRangePct / 200, 1 + pitchRangePct / 100, 1 + pitchRangePct / 200]) {
			const value = faderValueFromPitchRatio(ratio, pitchRangePct);
			assert.ok(value >= 0 && value <= 1, `value ${value} must stay within fader travel`);
			assert.ok(
				Math.abs(pitchRatioFromFaderValue(value, pitchRangePct) - ratio) < 1e-9,
				`round-trip mismatch for ratio ${ratio} at range +-${pitchRangePct}%`
			);
		}
	}
});

test('a ratio outside the selected range clamps its fader value to the nearer end', () => {
	assert.equal(faderValueFromPitchRatio(1.5, 8), 1);
	assert.equal(faderValueFromPitchRatio(0.5, 8), 0);
});

test('invalid inputs are rejected rather than silently coerced', () => {
	assert.throws(() => pitchRatioFromFaderValue(0.5, 0), /pitchRangePct/);
	assert.throws(() => pitchRatioFromFaderValue(0.5, -8), /pitchRangePct/);
	assert.throws(() => pitchRatioFromFaderValue(Number.NaN, 8), /value/);
	assert.throws(() => faderValueFromPitchRatio(0, 8), /ratio/);
	assert.throws(() => faderValueFromPitchRatio(-1, 8), /ratio/);
	assert.throws(() => faderValueFromPitchRatio(1, 0), /pitchRangePct/);
});
