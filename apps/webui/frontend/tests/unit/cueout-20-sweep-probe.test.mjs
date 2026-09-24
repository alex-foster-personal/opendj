// requirement: CUEOUT-14 (ChirpSync round 6)
// [if] the calibration probe correlates with itself at a non-zero offset [then] an echo can pull the lag one period away
// [if] a strong echo trails the probe by the old 60 ms click period [then] the sweep still reports the true lag
// [if] the sweep is built at 44.1 or 48 kHz [then] it has the named duration and never clips
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let cueLatency;

before(async () => {
	cueLatency = await loadTypeScriptModule('src/lib/player/cue-latency.ts');
});

function worstSidelobe(reference, sampleRate, guardMs) {
	const guard = Math.round((guardMs / 1000) * sampleRate);
	let energy = 0;
	for (const v of reference) energy += v * v;
	let worst = 0;
	for (let lag = guard; lag < reference.length; lag += 1) {
		let dot = 0;
		for (let i = 0; i + lag < reference.length; i += 1) dot += reference[i] * reference[i + lag];
		worst = Math.max(worst, Math.abs(dot) / energy);
	}
	return worst;
}

test('the sweep has no strong self-correlation away from zero, unlike the periodic click train', () => {
	const rate = 44100;
	const sweep = cueLatency.cueLatencySweep({ sampleRate: rate });
	const train = cueLatency.cueLatencyClickTrain({ sampleRate: rate });
	const sweepSide = worstSidelobe(sweep, rate, 3);
	const trainSide = worstSidelobe(train, rate, 3);
	assert.ok(trainSide > 0.5, `control: the click train's period sidelobe should be strong, got ${trainSide.toFixed(2)}`);
	assert.ok(sweepSide < 0.2, `if the sweep correlates ${sweepSide.toFixed(2)} with itself off zero then echoes can still pull the lag - broken`);
});

test('a strong echo 60 ms after the probe does not move the measured lag', () => {
	const rate = 44100;
	const sweep = cueLatency.cueLatencySweep({ sampleRate: rate });
	const trueLagMs = 137;
	const at = Math.round((trueLagMs / 1000) * rate);
	const echoAt = at + Math.round(0.06 * rate);
	const captured = new Float32Array(at + sweep.length + Math.round(0.4 * rate));
	for (let i = 0; i < sweep.length; i += 1) {
		captured[at + i] += 0.5 * sweep[i];
		captured[echoAt + i] += 0.45 * sweep[i];
	}
	let seed = 7;
	for (let i = 0; i < captured.length; i += 1) {
		seed = (seed * 1103515245 + 12345) % 2147483648;
		captured[i] += 0.02 * (seed / 2147483648 - 0.5);
	}
	const { lagMs } = cueLatency.crossCorrelateLagMs(sweep, captured, rate);
	assert.ok(Math.abs(lagMs - trueLagMs) <= 1, `if a 60 ms echo moves the lag to ${lagMs.toFixed(1)} ms then calibration jumps a period - broken`);
});

test('the sweep has its named duration at both common rates and stays inside full scale', () => {
	for (const rate of [44100, 48000]) {
		const sweep = cueLatency.cueLatencySweep({ sampleRate: rate });
		assert.equal(sweep.length, Math.round((cueLatency.CUE_LATENCY_SWEEP_MS / 1000) * rate));
		assert.ok(sweep.every((v) => Math.abs(v) <= 1), 'if the sweep clips then the speaker distorts the probe - broken');
		assert.equal(sweep[0], 0, 'if the sweep does not fade in then it clicks, which is a periodic-free but broadband transient - broken');
	}
	assert.throws(() => cueLatency.cueLatencySweep({ sampleRate: 0 }), /sampleRate/);
});
