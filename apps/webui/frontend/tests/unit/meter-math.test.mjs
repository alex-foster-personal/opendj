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
let calFields;

before(async () => {
	meter = await loadTypeScriptModule('src/lib/rb/meter-math.ts');
	calFields = await loadTypeScriptModule('src/lib/rb/prefs-fields.ts');
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

// --- calibration -----------------------------------------------------------
//
// Regression lines:
// - if an uncalibrated install stops using the default scale then every
//   existing user's meter silently changes under them
// - if calibration does not SHIFT the whole scale then the PPM spacing is
//   replaced by an invented curve and the segments stop meaning dB steps
// - if the calibrated red point does not become the first red segment then
//   the number the user chose by ear is not the number the meter uses

test('null calibration keeps the default scale exactly', () => {
	assert.deepEqual(
		[...meter.segmentThresholdsForRed(null)],
		[...meter.SEGMENT_THRESHOLDS_DBFS]
	);
});

test('the calibrated level becomes the first RED segment', () => {
	for (const red of [-12, -6, -3, 0, 3]) {
		const t = meter.segmentThresholdsForRed(red);
		// Segment 8 is the first red one; thresholds are 0-indexed.
		assert.ok(
			Math.abs(t[7] - red) < 1e-9,
			`red anchor ${red} landed at ${t[7]}`
		);
		assert.equal(meter.segmentBand(8), 'red');
	}
});

test('calibration shifts the whole scale, preserving PPM spacing', () => {
	const base = meter.SEGMENT_THRESHOLDS_DBFS;
	const shifted = meter.segmentThresholdsForRed(meter.DEFAULT_RED_DBFS + 6);
	for (let i = 0; i < base.length; i += 1) {
		assert.ok(Math.abs(shifted[i] - base[i] - 6) < 1e-9, `segment ${i} spacing changed`);
	}
});

test('a hot library reads mid-scale once calibrated, instead of pinning', () => {
	// Measured: the maintainer's tracks median +1.0 dBTP. On the default scale that is
	// 10/10 lit. Calibrating red to 0 dBFS must stop it pinning every track.
	const hot = 1.0;
	assert.equal(meter.segmentsLitFromDbfs(hot), 10, 'default scale should pin, that is the bug');
	const calibrated = meter.segmentThresholdsForRed(3);
	const lit = meter.segmentsLitFromDbfs(hot, calibrated);
	assert.ok(lit < 10, `calibrated scale still pinned at ${lit}/10`);
	assert.ok(lit >= 5, `calibrated scale collapsed to ${lit}/10, should stay readable`);
});

test('a wrong-length threshold set is a hard error, not a silent miscount', () => {
	assert.throws(() => meter.segmentsLitFromDbfs(-10, [-20, -10]), RangeError);
});

test('a non-finite calibration is rejected rather than shifting by NaN', () => {
	assert.throws(() => meter.segmentThresholdsForRed(NaN), RangeError);
	assert.throws(() => meter.segmentThresholdsForRed(Infinity), RangeError);
});

test('a low calibration never lights a segment at true silence', () => {
	// Issue #1578: an unclamped shift moves thresholds below METER_FLOOR_DBFS,
	// so a low red anchor (e.g. a quiet room during calibration) lit segments
	// with no signal at all. Sweep the FULL range the UI permits for
	// red_dbfs (prefs-fields.ts CAL_MIN_DBFS..CAL_MAX_DBFS), not samples: the
	// value comes off a live meter read, so it can land anywhere in the range
	// including a fractional boundary.
	const { CAL_MIN_DBFS, CAL_MAX_DBFS } = calFields;
	const STEP = 0.1;
	const values = [];
	for (let x = CAL_MIN_DBFS; x <= CAL_MAX_DBFS; x += STEP) values.push(x);
	values.push(CAL_MIN_DBFS, CAL_MAX_DBFS, -29.05);
	for (const red of values) {
		const thresholds = meter.segmentThresholdsForRed(red);
		const lit = meter.segmentsLitFromDbfs(meter.METER_FLOOR_DBFS, thresholds);
		assert.equal(
			lit,
			0,
			`red=${red} lit ${lit}/10 segments at the floor (${meter.METER_FLOOR_DBFS} dBFS)`
		);
	}
});

test('a low enough calibration clamps multiple thresholds to the same floor value', () => {
	// Codex P1 on #1578: the floor clamp above makes segmentThresholdsForRed
	// produce DUPLICATE values by design once redDbfs is low enough that more
	// than one shifted threshold lands at or below METER_FLOOR_DBFS - they all
	// clamp to the same number. ChannelLevelMeter.svelte's {#each} keys on
	// segment INDEX, not threshold, specifically because of this. This test
	// pins that the duplicate-value case is real (not hypothetical) and stays
	// reachable within the UI-permitted range, so a future change that
	// "simplifies" the template back to keying on threshold has something to
	// fail against.
	const { CAL_MIN_DBFS } = calFields;
	const thresholds = meter.segmentThresholdsForRed(CAL_MIN_DBFS);
	const atFloor = thresholds.filter((t) => t === meter.METER_FLOOR_DBFS);
	assert.ok(
		atFloor.length >= 2,
		`expected >= 2 thresholds clamped to the floor at CAL_MIN_DBFS=${CAL_MIN_DBFS}, got ${atFloor.length} of [${thresholds.join(', ')}]`
	);
	assert.notEqual(
		new Set(thresholds).size,
		thresholds.length,
		'thresholds should contain duplicates at this calibration, proving segment.threshold is unsafe as an each-block key'
	);
});
