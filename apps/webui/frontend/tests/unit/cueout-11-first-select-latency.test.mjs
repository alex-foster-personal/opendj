// requirement: CUEOUT-11
// [if] a Bluetooth-looking HEADPHONE CUE device is selected for the first time this session [then] shouldCalibrateCueLatency is true
// [if] the cue label is WH-1000XM5 with no word Bluetooth [then] shouldCalibrateCueLatency is true
// [if] the same cue device is selected again after a successful cal [then] shouldCalibrateCueLatency is false
// [if] the same cue device is selected again after a failed cal [then] shouldCalibrateCueLatency is true
// [if] the cue label is External Headphones or speakers [then] shouldCalibrateCueLatency is false
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

test('shouldCalibrateCueLatency is first CUE select of BT/unnamed BT, retry after fail, never speakers or the Mac jack', () => {
	assert.equal(
		headphones.shouldCalibrateCueLatency({
			previousCueId: null,
			nextCueId: 'bt-1',
			label: 'WH-1000XM5 (Bluetooth)',
			alreadyCalibrated: false
		}),
		true
	);
	assert.equal(
		headphones.shouldCalibrateCueLatency({
			previousCueId: null,
			nextCueId: 'sony',
			label: 'WH-1000XM5',
			alreadyCalibrated: false
		}),
		true,
		'Chrome often omits the word Bluetooth from the device label'
	);
	assert.equal(
		headphones.shouldCalibrateCueLatency({
			previousCueId: 'bt-1',
			nextCueId: 'bt-1',
			label: 'AirPods Pro',
			alreadyCalibrated: false
		}),
		true,
		'failed first cal must retry on the same live sink without a second setSinkId'
	);
	assert.equal(
		headphones.shouldCalibrateCueLatency({
			previousCueId: 'bt-1',
			nextCueId: 'bt-1',
			label: 'AirPods Pro',
			alreadyCalibrated: true
		}),
		false
	);
	assert.equal(
		headphones.shouldCalibrateCueLatency({
			previousCueId: null,
			nextCueId: 'wired',
			label: 'External Headphones',
			alreadyCalibrated: false
		}),
		false
	);
	assert.equal(
		headphones.shouldCalibrateCueLatency({
			previousCueId: null,
			nextCueId: 'speakers',
			label: 'MacBook Pro Speakers',
			alreadyCalibrated: false
		}),
		false
	);
	assert.equal(
		headphones.shouldCalibrateCueLatency({
			previousCueId: null,
			nextCueId: 'blank',
			label: '',
			alreadyCalibrated: false
		}),
		false
	);
	assert.equal(
		headphones.shouldCalibrateCueLatency({
			previousCueId: 'wired',
			nextCueId: 'bt-2',
			label: 'AirPods G2',
			alreadyCalibrated: false
		}),
		true
	);
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

test('selectHeadphoneOutput wires first-select cal through the cue destination, not the delay line', () => {
	const source = readFileSync(`${FRONTEND}/src/lib/player/headphones.ts`, 'utf8');
	assert.match(source, /shouldCalibrateCueLatency/);
	assert.match(source, /measureCueLatencyMs/);
	assert.match(source, /unlockAudioInputConstraints/);
	assert.match(source, /src\.connect\(nodes\.destination\)/);
	assert.match(source, /_calibratedCueIds/);
	assert.match(source, /_calibratingCueId/);
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
