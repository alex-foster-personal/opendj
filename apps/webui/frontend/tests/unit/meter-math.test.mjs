import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// PPM level metering policy. The worklet measures; every number below is
// decided here, so this file is where the meter's behaviour is actually pinned.
//
// Regression lines:
// - if segment thresholds become linear in amplitude then the bottom eight
//   segments never light for real music and the bar is an on/off lamp
// - if attack stops being instantaneous then a kick transient never lights
//   the meter and the whole point of a PPM is gone
// - if the decay rate stops being per-second then the fall speed changes with
//   the display refresh rate and a 144Hz screen reads differently to a 60Hz one
// - if silence returns -Infinity then every downstream clamp produces NaN
// - if the clip threshold moves to exactly 0.0 then a clipped file that peaks
//   at full scale reads as merely loud

let meter;

before(async () => {
	meter = await loadTypeScriptModule('src/lib/rb/meter-math.ts');
});

test('digital silence floors instead of diverging to -Infinity', () => {
	const db = meter.dbfsFromAmplitude(0);
	assert.ok(Number.isFinite(db), `expected finite, got ${db}`);
	assert.ok(db < -100, `expected a deep floor, got ${db}`);
});

test('full scale is 0 dBFS and half amplitude is -6 dBFS', () => {
	assert.ok(Math.abs(meter.dbfsFromAmplitude(1)) < 1e-9);
	assert.ok(Math.abs(meter.dbfsFromAmplitude(0.5) + 6.0206) < 1e-3);
});

test('every halving of amplitude is another 6 dB down', () => {
	const steps = [1, 0.5, 0.25, 0.125, 0.0625].map((a) => meter.dbfsFromAmplitude(a));
	for (let i = 1; i < steps.length; i += 1) {
		assert.ok(
			Math.abs(steps[i - 1] - steps[i] - 6.0206) < 1e-3,
			`step ${i} was ${steps[i - 1] - steps[i]} dB, expected 6.02`
		);
	}
});

test('amplitude and dBFS round-trip', () => {
	// The inverse lives here rather than in the module: nothing in src needs it,
	// and an export with no consumer is what the unused-exports gate counts.
	const amplitudeFromDbfs = (db) => 10 ** (db / 20);
	for (const db of [-60, -24, -12, -6, -1.5, 0]) {
		const back = meter.dbfsFromAmplitude(amplitudeFromDbfs(db));
		assert.ok(Math.abs(back - db) < 1e-9, `${db} round-tripped to ${back}`);
	}
});

test('segment thresholds are monotonic and logarithmically spaced', () => {
	const t = meter.SEGMENT_THRESHOLDS_DBFS;
	assert.equal(t.length, 10);
	for (let i = 1; i < t.length; i += 1) {
		assert.ok(t[i] > t[i - 1], `threshold ${i} (${t[i]}) is not above ${t[i - 1]}`);
	}
	// The defining property: steps SHRINK toward the top, because the eye and
	// the danger are both concentrated near full scale. Equal steps would mean
	// linear-in-dB, which is not what a mixer does.
	assert.ok(t[1] - t[0] > t[9] - t[8], 'spacing does not tighten toward 0 dBFS');
});

test('segments light progressively across the whole range', () => {
	assert.equal(meter.segmentsLitFromDbfs(-60), 0);
	assert.equal(meter.segmentsLitFromDbfs(-34), 1);
	assert.equal(meter.segmentsLitFromDbfs(-12), 5);
	assert.equal(meter.segmentsLitFromDbfs(0), 10);
	// Real music at a sane level must use the middle of the bar, not the bottom.
	const lit = meter.segmentsLitFromDbfs(-9);
	assert.ok(lit >= 5 && lit <= 7, `-9 dBFS lit ${lit} segments, expected mid-scale`);
});

test('color bands are green low, amber middle, red top', () => {
	assert.equal(meter.segmentBand(1), 'green');
	assert.equal(meter.segmentBand(4), 'green');
	assert.equal(meter.segmentBand(5), 'amber');
	assert.equal(meter.segmentBand(7), 'amber');
	assert.equal(meter.segmentBand(8), 'red');
	assert.equal(meter.segmentBand(10), 'red');
});

test('a segment outside 1..10 is a hard error, not a silent clamp', () => {
	assert.throws(() => meter.segmentBand(0), RangeError);
	assert.throws(() => meter.segmentBand(11), RangeError);
	assert.throws(() => meter.segmentBand(2.5), RangeError);
});

test('attack is instantaneous', () => {
	assert.equal(meter.stepBallistics(-40, -6, 0.001), -6);
	assert.equal(meter.stepBallistics(-40, -6, 0), -6);
});

test('decay is a fixed rate per second, not per frame', () => {
	// Same wall-clock elapsed, different frame counts, must land identically.
	const oneStep = meter.stepBallistics(0, -60, 0.1);
	let stepwise = 0;
	for (let i = 0; i < 10; i += 1) stepwise = meter.stepBallistics(stepwise, -60, 0.01);
	assert.ok(
		Math.abs(oneStep - stepwise) < 1e-9,
		`60Hz gave ${stepwise}, one 0.1s step gave ${oneStep}`
	);
});

test('decay matches the EBU PPM fallback of 20 dB in 1.7 s', () => {
	const after = meter.stepBallistics(0, -60, 1.7);
	assert.ok(Math.abs(after + 20) < 1e-6, `fell to ${after} after 1.7s, expected -20`);
});

test('decay never falls below the level actually present', () => {
	assert.equal(meter.stepBallistics(-10, -12, 10), -12);
});

test('negative or non-finite elapsed time is a hard error', () => {
	assert.throws(() => meter.stepBallistics(-10, -20, -0.1), RangeError);
	assert.throws(() => meter.stepBallistics(-10, -20, NaN), RangeError);
});

test('peak hold parks at the maximum then falls', () => {
	let state = meter.stepPeakHold(meter.INITIAL_PEAK_HOLD, -3, 0.016);
	assert.equal(state.db, -3);
	// Still inside the hold window: the marker must not move at all.
	state = meter.stepPeakHold(state, -40, 0.5);
	assert.equal(state.db, -3, 'peak marker fell during the hold window');
	// Past the hold window: it must start falling.
	state = meter.stepPeakHold(state, -40, 0.9);
	assert.ok(state.db < -3, 'peak marker never released after the hold window');
});

test('a new maximum restarts the hold', () => {
	let state = meter.stepPeakHold(meter.INITIAL_PEAK_HOLD, -20, 0.016);
	state = meter.stepPeakHold(state, -40, 0.9);
	state = meter.stepPeakHold(state, -6, 0.016);
	assert.equal(state.db, -6);
	assert.equal(state.heldS, 0, 'hold timer did not reset on a new peak');
});

test('normalized position spans the floor to full scale', () => {
	assert.equal(meter.normalizedFromDbfs(meter.METER_FLOOR_DBFS), 0);
	assert.equal(meter.normalizedFromDbfs(0), 1);
	assert.equal(meter.normalizedFromDbfs(-120), 0, 'below floor did not clamp');
	assert.equal(meter.normalizedFromDbfs(6), 1, 'above full scale did not clamp');
	const mid = meter.normalizedFromDbfs(-30);
	assert.ok(mid > 0.4 && mid < 0.6, `-30 dBFS mapped to ${mid}`);
});

test('clip latches just below full scale, not at it', () => {
	assert.equal(meter.isClipping(-1), false);
	assert.equal(meter.isClipping(0), true);
	assert.ok(meter.CLIP_DBFS < 0, 'clip threshold sits at exactly 0 dBFS');
	assert.equal(meter.isClipping(meter.CLIP_DBFS), true);
});

test('the worklet processor holds no policy numbers', () => {
	// The split only holds if the thresholds live on this side. If a number
	// from meter-math is ever pasted into the processor, this catches it.
	const processor = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/meter-processor.js', import.meta.url)),
		'utf8'
	);
	const code = processor.slice(processor.indexOf('class MeterProcessor'));
	for (const threshold of [-34, -26, -20, -16, -12, -9, -6, -3, -1.5]) {
		assert.ok(
			!code.includes(String(threshold)),
			`processor code contains the policy number ${threshold}`
		);
	}
	assert.ok(!code.includes('log10'), 'processor is converting to dB, which is policy');
});
