// requirement: CUEOUT-11, IOPIN-08
// (CUEOUT-14 replaced the first-select auto-chirp with the CALIBRATE modal; the
// shouldCalibrateCueLatency / label-skip lines moved to cueout-14-alignment-modes.)
// [if] a delayed chirp is captured at N ms with a strong peak [then] measureCueLatencyMs returns N
// [if] the peak is weaker than CUE_LATENCY_PEAK_MIN [then] resolveCalibratedHeadDelayMs throws and HEAD DELAY is not written
// [if] measured lag is outside 0-500 ms [then] resolveCalibratedHeadDelayMs throws
// [if] AUDIO IN would be a headphone/HFP mic [then] unlockAudioInputConstraints still prefers the built-in mic
// [if] the live cue sink is selected again [then] cueOutputChangePlan.skip is true (no second setSinkId)
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { addGaussianNoise, delayedCapture } from './cue-latency-eval.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const SAMPLE_RATE = 48000;

let cueLatency;
let headphones;

before(async () => {
	cueLatency = await loadTypeScriptModule('src/lib/player/cue-latency.ts');
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
});

test('measureCueLatencyMs recovers 0, 40, 180, 500 ms of injected lag', async () => {
	for (const delayMs of [0, 40, 180, 500]) {
		const measured = await cueLatency.measureCueLatencyMs({
			sampleRate: SAMPLE_RATE,
			playAndRecord: async (reference) => delayedCapture(reference, delayMs, SAMPLE_RATE)
		});
		assert.equal(measured, delayMs, `expected ${delayMs} ms, got ${measured}`);
	}
});

test('weak peak and out-of-range lag fail fast without a HEAD DELAY integer', () => {
	assert.throws(
		() => cueLatency.resolveCalibratedHeadDelayMs(180, 0.1),
		/too weak to trust/
	);
	assert.throws(
		() => cueLatency.resolveCalibratedHeadDelayMs(501, 0.99),
		/outside 0\.\.500/
	);
	assert.throws(
		() => cueLatency.resolveCalibratedHeadDelayMs(-2, 0.99),
		/outside 0\.\.500/
	);
	assert.equal(cueLatency.resolveCalibratedHeadDelayMs(179.6, 0.99), 180);
	assert.equal(cueLatency.resolveCalibratedHeadDelayMs(0.4, 0.99), 0);
	assert.equal(cueLatency.resolveCalibratedHeadDelayMs(500, 0.99), 500);
});

test('noise-only capture fails instead of inventing a delay', async () => {
	await assert.rejects(
		() =>
			cueLatency.measureCueLatencyMs({
				sampleRate: SAMPLE_RATE,
				playAndRecord: async (reference) =>
					addGaussianNoise(new Float32Array(reference.length + 8000), 0.4, 7)
			}),
		/too weak to trust|outside 0/
	);
});

test('cueOutputChangePlan skips a live re-select and parallel-pins on a real change', () => {
	assert.deepEqual(
		headphones.cueOutputChangePlan({
			currentCueId: 'hp',
			nextCueId: 'hp',
			currentActive: true
		}),
		{ skip: true, applyMixBeforePlay: false, pinMasterInParallel: false }
	);
	assert.deepEqual(
		headphones.cueOutputChangePlan({
			currentCueId: null,
			nextCueId: 'hp',
			currentActive: false
		}),
		{ skip: false, applyMixBeforePlay: true, pinMasterInParallel: true }
	);
	assert.deepEqual(
		headphones.cueOutputChangePlan({
			currentCueId: 'wired',
			nextCueId: 'bt',
			currentActive: true
		}),
		{ skip: false, applyMixBeforePlay: true, pinMasterInParallel: true }
	);
});

test('sinkSelectIsNoop is the OUT-change lag guard', () => {
	assert.equal(headphones.sinkSelectIsNoop('hp', 'hp', true), true);
	assert.equal(headphones.sinkSelectIsNoop('hp', 'bt', true), false);
	assert.equal(headphones.sinkSelectIsNoop(null, 'hp', false), false);
	assert.equal(headphones.sinkSelectIsNoop('speakers', 'speakers', true), true);
});

test('calibration capture never constrains getUserMedia to a handsfree mic', () => {
	const devices = [
		{ kind: 'audioinput', deviceId: 'airpods-mic', label: 'AirPods Microphone' },
		{ kind: 'audioinput', deviceId: 'builtin-mic', label: 'MacBook Pro Microphone' }
	];
	const audio = headphones.unlockAudioInputConstraints(devices, 'airpods-mic');
	assert.deepEqual(audio.deviceId, { ideal: 'builtin-mic' });
	assert.equal(headphones.preferredAudioInputDeviceId(devices), 'builtin-mic');
});

test('the calibration chirp reaches the cue destination, not the delay line, and select no longer auto-chirps', () => {
	const source = readFileSync(`${FRONTEND}/src/lib/player/headphones.ts`, 'utf8');
	assert.match(source, /cueAlignAudioEffects/);
	assert.match(source, /unlockAudioInputConstraints/);
	assert.match(source, /bus === 'master' \? ctx\.destination : nodes\.destination/);
	assert.doesNotMatch(source, /shouldCalibrateCueLatency|_calibratedCueIds|_calibratingCueId|measureCueLatencyMs/,
		'CUEOUT-14: the first-select auto-chirp is gone; CALIBRATE is the only chirp path');
	assert.match(source, /sinkSelectIsNoop/);
	assert.match(source, /cueOutputChangePlan/);
	assert.match(source, /Promise\.all/);
	assert.doesNotMatch(
		source,
		/src\.connect\(nodes\.delay\)/,
		'cal chirp must bypass HEAD DELAY or the measurement double-counts it'
	);
});

test('CUE_LATENCY_PEAK_MIN stays 0.35 so a mutation to 0.99 cannot silently pass', () => {
	assert.equal(cueLatency.CUE_LATENCY_PEAK_MIN, 0.35);
});

test('DSP guards fail fast on empty, short, or silent input', () => {
	assert.throws(() => cueLatency.cueLatencyClickTrain({ sampleRate: 0 }), /sampleRate/);
	assert.throws(
		() => cueLatency.crossCorrelateLagMs(new Float32Array(0), new Float32Array(8), SAMPLE_RATE),
		/reference is empty/
	);
	assert.throws(
		() => cueLatency.crossCorrelateLagMs(new Float32Array(8), new Float32Array(4), SAMPLE_RATE),
		/shorter than the reference/
	);
	assert.throws(() => cueLatency.cueLatencyCaptureMs(-1), /capture window/);
});

test('captured signal level is calculated from the received samples, with no output claim', () => {
	assert.deepEqual(cueLatency.capturedSignalLevel(new Float32Array([0, 1, -1, 0])), {
		rms: Math.sqrt(0.5),
		peak: 1
	});
	assert.deepEqual(cueLatency.capturedSignalLevel(new Float32Array(8)), { rms: 0, peak: 0 });
});
