// requirement: CUEOUT-4988
// [if] selectAudioOutput exists but rejects NotSupportedError [then] I/O falls through to enumerate-and-unlock instead of toasting acquisition failed
// [if] the operator cancels the chooser with AbortError [then] it does not silently enumerate, and CUEOUT-4916 names a cancelled picker instead of throwing
// [if] selectAudioOutput rejects NotAllowedError [then] it is not treated as an unsupported chooser, and CUEOUT-4916 names a blocked picker instead of throwing
//
// Regression: Sentry OPEN-DJ-BE-73 "Error: headphone output acquisition failed: Not supported".
// Chromium can expose navigator.mediaDevices.selectAudioOutput as a function and still reject
// with DOMException NotSupportedError ("Not supported") when the Audio Output Devices API is
// gated or incomplete. The chooser branch used to wrap that as a hard toast/Sentry event
// instead of using the unlock_and_enumerate path Chrome without the chooser already uses.
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let headphones;
let playerState;

const UNLABELLED = [
	{ kind: 'audiooutput', deviceId: 'default', label: '' },
	{ kind: 'audioinput', deviceId: 'default', label: '' }
];
const LABELLED = [
	{ kind: 'audiooutput', deviceId: 'speakers', label: 'MacBook Pro Speakers' },
	{ kind: 'audiooutput', deviceId: 'over-ears', label: "a friend's over-ears" },
	{ kind: 'audiooutput', deviceId: 'iface', label: 'Scarlett 2i2' },
	{ kind: 'audioinput', deviceId: 'builtin-mic', label: 'MacBook Pro Microphone' }
];

const savedNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
const savedMediaElement = globalThis.HTMLMediaElement;
const savedAudioContext = globalThis.AudioContext;

function installFakeBrowser({ selectAudioOutput, getUserMedia }) {
	let unlocked = false;
	const mediaDevices = {
		enumerateDevices: async () => (unlocked ? LABELLED : UNLABELLED),
		getUserMedia: async (constraints) => {
			const stream = await getUserMedia(constraints);
			unlocked = true;
			return stream;
		},
		selectAudioOutput,
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
	globalThis.AudioContext = class {
		setSinkId() {
			return Promise.resolve();
		}
	};
}

function monitorSource() {
	// A running room context: main reopens it (suspend, then resume) after an output change.
	const context = {
		state: 'running',
		setSinkId: () => Promise.resolve(),
		suspend() {
			context.state = 'suspended';
			return Promise.resolve();
		},
		resume() {
			context.state = 'running';
			return Promise.resolve();
		},
		addEventListener() {},
		removeEventListener() {}
	};
	return { context, masterGain: {} };
}

function rejectWith(name, message) {
	return async () => {
		throw Object.assign(new Error(message), { name });
	};
}

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	playerState = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
});

afterEach(() => {
	if (savedNavigator === undefined) delete globalThis.navigator;
	else Object.defineProperty(globalThis, 'navigator', savedNavigator);
	globalThis.HTMLMediaElement = savedMediaElement;
	if (savedAudioContext === undefined) delete globalThis.AudioContext;
	else globalThis.AudioContext = savedAudioContext;
	const hp = playerState.mixerState.headphones;
	hp.outputs = [];
	hp.inputs = [];
	hp.error = null;
	hp.selected_output_device_id = null;
	hp.selected_master_output_device_id = null;
	hp.selected_input_device_id = null;
});

test('if selectAudioOutput rejects Not supported then I/O enumerates instead of failing', async () => {
	installFakeBrowser({
		selectAudioOutput: rejectWith('NotSupportedError', 'Not supported'),
		getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] })
	});
	await headphones.acquireHeadphoneOutput(monitorSource);
	const hp = playerState.mixerState.headphones;
	assert.deepEqual(
		hp.outputs.map((output) => output.label),
		['System default output', 'MacBook Pro Speakers', "a friend's over-ears", 'Scarlett 2i2']
	);
	assert.equal(hp.error, null);
});

test('if selectAudioOutput rejects only the Not supported message then I/O still enumerates', async () => {
	installFakeBrowser({
		selectAudioOutput: rejectWith('Error', 'Not supported'),
		getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] })
	});
	await headphones.acquireHeadphoneOutput(monitorSource);
	assert.equal(playerState.mixerState.headphones.error, null);
	assert.equal(playerState.mixerState.headphones.outputs.length, 4);
});

test('if selectAudioOutput rejects NotAllowedError then it is named, not enumerated as an unsupported chooser', async () => {
	installFakeBrowser({
		selectAudioOutput: rejectWith('NotAllowedError', 'Permission denied'),
		getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] })
	});
	await headphones.acquireHeadphoneOutput(monitorSource);
	const hp = playerState.mixerState.headphones;
	assert.equal(hp.outputs.length, 0, 'permission refusal must not quietly enumerate');
	// CUEOUT-4916: a blocked picker is a named supported state, not an acquisition failure.
	assert.equal(hp.error, headphones.OUTPUT_PROMPT_REFUSED_NOTICE);
});

test('if the operator aborts the chooser then it is named and nothing is enumerated', async () => {
	installFakeBrowser({
		selectAudioOutput: rejectWith('AbortError', 'The user aborted a request.'),
		getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] })
	});
	await headphones.acquireHeadphoneOutput(monitorSource);
	const hp = playerState.mixerState.headphones;
	assert.equal(hp.outputs.length, 0, 'a cancelled chooser must not quietly enumerate');
	// CUEOUT-4916: closing the picker is a named supported state, not an acquisition failure.
	assert.equal(hp.error, headphones.CHOOSER_CANCELLED_NOTICE);
});
