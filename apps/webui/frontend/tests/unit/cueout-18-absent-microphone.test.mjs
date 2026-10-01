// requirement: CUEOUT-12 (headphone output device acquisition)
// [if] the machine has no microphone [then] pressing I/O names the absence and leaves the sinks selectable, instead of reporting a failure
// [if] the microphone exists and the operator refuses it [then] the refusal still travels as a failure
// [if] the absence notice stops naming what is absent [then] the headphone probe can no longer tell absence from a bug
//
// Regression line: on a host with output sinks but no audio input (a Linux CI
// runner holding a PulseAudio null sink), getUserMedia rejects NotFoundError
// and the whole acquire path reported "headphone output acquisition failed:
// Requested device not found" -- a fault message for a machine with no fault.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = new URL('../../', import.meta.url);

let headphones;
let playerState;

const OUTPUT_ONLY = [{ kind: 'audiooutput', deviceId: 'null-sink', label: '' }];

const savedNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
const savedMediaElement = globalThis.HTMLMediaElement;

function installFakeBrowser({ getUserMedia, devices = OUTPUT_ONLY }) {
	const mediaDevices = {
		enumerateDevices: async () => devices,
		getUserMedia,
		addEventListener() {},
		removeEventListener() {}
	};
	Object.defineProperty(globalThis, 'navigator', {
		value: { mediaDevices },
		configurable: true,
		writable: true
	});
	globalThis.HTMLMediaElement = class {
		setSinkId() {
			return Promise.resolve();
		}
	};
}

function monitorSource() {
	return { context: { setSinkId: () => Promise.resolve() }, masterGain: {} };
}

function rejectWith(name, message) {
	return async () => {
		throw Object.assign(new Error(message), { name });
	};
}

/** The vocabulary the headphone probe accepts, read from the probe itself so
 * that renaming the notice here and forgetting the probe (or the reverse)
 * fails this test instead of failing in CI a day later. */
function probeAbsenceVocabulary() {
	const source = readFileSync(
		new URL('tests/e2e/performance-headphone-device-probe.spec.ts', FRONTEND),
		'utf8'
	);
	const match = source.match(/^[\t ]*(\/[^\n]*\/i)[\t ]*$/m);
	assert.ok(match !== null, 'the headphone probe must carry its absence vocabulary as one regex');
	return new RegExp(match[1].slice(1, -2), 'i');
}

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	playerState = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
});

afterEach(() => {
	if (savedNavigator === undefined) delete globalThis.navigator;
	else Object.defineProperty(globalThis, 'navigator', savedNavigator);
	globalThis.HTMLMediaElement = savedMediaElement;
	const hp = playerState.mixerState.headphones;
	hp.outputs = [];
	hp.inputs = [];
	hp.error = null;
	hp.selected_output_device_id = null;
	hp.selected_master_output_device_id = null;
	hp.selected_input_device_id = null;
});

test('if a missing device is read as a refusal then an absent mic looks like a broken one', () => {
	assert.equal(headphones.microphoneIsMissing({ name: 'NotFoundError' }), true);
	assert.equal(headphones.microphoneIsMissing({ name: 'DevicesNotFoundError' }), true);
});

test('if every rejection counts as missing then a real failure is swallowed', () => {
	for (const error of [
		{ name: 'NotAllowedError' },
		{ name: 'NotReadableError' },
		new Error('headphone getUserMedia timed out after 5000ms'),
		'NotFoundError',
		null,
		undefined
	]) {
		assert.equal(headphones.microphoneIsMissing(error), false, `error ${String(error)}`);
	}
});

test('if the machine has no microphone then I/O names the absence and keeps the sinks selectable', async () => {
	installFakeBrowser({ getUserMedia: rejectWith('NotFoundError', 'Requested device not found') });
	await headphones.acquireHeadphoneOutput(monitorSource);
	const hp = playerState.mixerState.headphones;
	assert.equal(hp.error, headphones.MIC_ABSENT_NOTICE);
	// IOPIN-14 lists the system default alongside it, so presence is asserted by id.
	assert.ok(
		hp.outputs.some((output) => output.id === 'null-sink' && output.label !== ''),
		'the enumerated sink must still be selectable, under a readable name'
	);
	assert.equal(hp.supported, true, 'the browser supports the API; only the hardware is absent');
});

test('if a refused microphone stops failing then a permission bug reads as normal', async () => {
	installFakeBrowser({ getUserMedia: rejectWith('NotAllowedError', 'Permission denied') });
	await assert.rejects(
		() => headphones.acquireHeadphoneOutput(monitorSource),
		/acquisition failed: .*Permission denied/
	);
	assert.match(playerState.mixerState.headphones.error ?? '', /acquisition failed/);
});

test('if a notice stops naming what is missing then the probe cannot tell it from a bug', () => {
	// Every notice this path can leave in headphones.error, not just the new
	// one: the probe reads that field and has no other way to separate an
	// explained absence from an unexplained failure.
	const vocabulary = probeAbsenceVocabulary();
	for (const notice of [headphones.MIC_ABSENT_NOTICE, headphones.MIC_DECLINED_NOTICE]) {
		assert.match(notice, vocabulary);
		assert.doesNotMatch(notice, /fail|error/i, 'an absence is a state, not a failure');
	}
	assert.notEqual(
		headphones.MIC_ABSENT_NOTICE,
		headphones.MIC_DECLINED_NOTICE,
		'a refusal the operator can undo and hardware they cannot must read differently'
	);
});
