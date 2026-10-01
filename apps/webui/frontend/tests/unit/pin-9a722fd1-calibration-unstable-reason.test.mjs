/**
 * pin 9a722fd1 (wording part): calibration said "try again with less room
 * noise" when there was no room noise. That sentence was hard-coded onto every
 * run whose three offsets disagreed, whatever the measurements said. The
 * unstable failure now names the reason the measurements actually support.
 *
 * - if a clipped capture is blamed on the room then the operator quiets a
 *   room that was never the problem -> broken
 * - if a barely-heard chirp is reported as a latency change then the operator
 *   goes hunting a device fault instead of turning the output up -> broken
 * - if clearly-heard chirps with moving lags are reported as noise then the
 *   wireless link that caused it is never suspected -> broken
 * - if the message names the bus that held steady -> broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let unstable;
before(async () => {
	unstable = await loadTypeScriptModule('src/lib/player/cue-align-unstable.ts');
});

const LIMITS = { comfortablePeak: 0.5, maxSpreadMs: 10 };

function runs(lags, peak = 0.9, clippedFraction = 0) {
	return lags.map((lagMs) => ({ lagMs, peak, clippedFraction }));
}

test('clearly heard chirps whose lag moved on one bus name that bus and rule out noise', () => {
	const result = unstable.diagnoseUnstableMeasurement(
		runs([200, 200, 200]),
		runs([900, 915, 900]),
		LIMITS
	);
	assert.equal(result.reason, 'path_latency_moved');
	assert.equal(result.bus, 'cue');
	assert.match(result.message, /the headphones latency itself changed between runs/);
	assert.match(result.message, /Every chirp was heard clearly \(weakest peak 0\.90\)/);
	assert.doesNotMatch(result.message, /room noise is the cause|less room noise/);
});

test('the speakers are named when they are the bus that moved', () => {
	const result = unstable.diagnoseUnstableMeasurement(
		runs([200, 230, 200]),
		runs([900, 900, 900]),
		LIMITS
	);
	assert.equal(result.reason, 'path_latency_moved');
	assert.equal(result.bus, 'master');
	assert.match(result.message, /the speakers latency itself changed between runs/);
});

test('both buses moving is reported as both, not pinned on one', () => {
	const result = unstable.diagnoseUnstableMeasurement(
		runs([200, 230, 205]),
		runs([900, 905, 940]),
		LIMITS
	);
	assert.equal(result.reason, 'path_latency_moved');
	assert.equal(result.bus, 'both');
	assert.match(result.message, /both outputs/);
});

test('a chirp heard below the comfortable peak is reported as too quiet, with the figure', () => {
	const cue = runs([900, 915, 900]);
	cue[1].peak = 0.37;
	const result = unstable.diagnoseUnstableMeasurement(runs([200, 200, 200]), cue, LIMITS);
	assert.equal(result.reason, 'signal_too_quiet');
	assert.equal(result.bus, 'cue');
	assert.match(result.message, /the headphones chirp was only just heard \(weakest peak 0\.37, comfortable is 0\.5\)/);
});

test('a clipped capture outranks every other reason and names the bus', () => {
	const master = runs([200, 200, 200]);
	master[2].clippedFraction = 0.02;
	const cue = runs([900, 915, 900]);
	cue[1].peak = 0.37;
	const result = unstable.diagnoseUnstableMeasurement(master, cue, LIMITS);
	assert.equal(result.reason, 'input_clipped');
	assert.equal(result.bus, 'master');
	assert.match(result.message, /the microphone clipped while recording the speakers \(2\.0% of samples at full scale\)/);
});

test('clippedFraction counts full-scale samples and nothing else', () => {
	assert.equal(unstable.clippedFraction(new Float32Array([0, 0.5, -0.9, 0.98])), 0);
	assert.equal(unstable.clippedFraction(new Float32Array([1, -1, 0.995, 0])), 0.75);
	assert.throws(() => unstable.clippedFraction(new Float32Array(0)), RangeError);
});

test('a capture counts as clipped only above the tolerated fraction', () => {
	const justUnder = runs([200, 200, 200]);
	justUnder[0].clippedFraction = unstable.CLIPPED_FRACTION_MAX;
	const result = unstable.diagnoseUnstableMeasurement(justUnder, runs([900, 915, 900]), LIMITS);
	assert.equal(result.reason, 'path_latency_moved', 'the threshold is exclusive');
});

test('the named error carries the reason and the classic prefix and per-bus lags', () => {
	const error = new unstable.CueAlignMeasurementUnstable(
		15,
		runs([200, 200, 200]),
		runs([900, 915, 900]),
		LIMITS
	);
	assert.equal(error.name, 'CueAlignMeasurementUnstable');
	assert.equal(error.reason, 'path_latency_moved');
	assert.ok(error instanceof Error);
	assert.match(error.message, /^measurement unstable \(spread 15 ms\): /);
	assert.match(error.message, /\(speakers 200, 200, 200 ms; headphones 900, 915, 900 ms\)$/);
});
