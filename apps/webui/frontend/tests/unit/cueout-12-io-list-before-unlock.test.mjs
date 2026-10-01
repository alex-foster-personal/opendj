// requirement: CUEOUT-12 (I/O device menu lists devices even when the label unlock fails)
// [if] the label-unlock getUserMedia rejects or hangs [then] the I/O selects still list every enumerated output and input
// [if] the label-unlock getUserMedia fails [then] mixerState.headphones.error names the failure instead of leaving an empty menu silently
// [if] the label unlock succeeds [then] the lists carry the labelled devices from the post-unlock enumeration
//
// Regression line: if clicking I/O awaits getUserMedia before enumerating then a hung or denied
// permission prompt leaves MASTER / HEADPHONE CUE / AUDIO IN with only their placeholders
// (observed live Tue 15 Sep 2026: "headphone getUserMedia timed out after 5000ms", selects empty).
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
	{ kind: 'audiooutput', deviceId: 'over-ears', label: "Steve's over-ears" },
	{ kind: 'audioinput', deviceId: 'builtin-mic', label: 'MacBook Pro Microphone' }
];

const savedNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
const savedMediaElement = globalThis.HTMLMediaElement;

function installFakeBrowser({ getUserMedia }) {
	let unlocked = false;
	const mediaDevices = {
		enumerateDevices: async () => (unlocked ? LABELLED : UNLABELLED),
		getUserMedia: async (constraints) => {
			const stream = await getUserMedia(constraints);
			unlocked = true;
			return stream;
		},
		addEventListener() {},
		removeEventListener() {}
	};
	Object.defineProperty(globalThis, 'navigator', { value: { mediaDevices }, configurable: true, writable: true });
	globalThis.HTMLMediaElement = class {
		setSinkId() {
			return Promise.resolve();
		}
	};
}

function monitorSource() {
	return { context: { setSinkId: () => Promise.resolve() }, masterGain: {} };
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

test('if the label unlock is denied then the I/O menu still lists devices and shows the error', async () => {
	installFakeBrowser({
		getUserMedia: async () => {
			throw Object.assign(new Error('Permission denied'), { name: 'NotAllowedError' });
		}
	});
	await assert.rejects(() => headphones.acquireHeadphoneOutput(monitorSource), /Permission denied/);
	const hp = playerState.mixerState.headphones;
	assert.equal(hp.outputs.length, 1, 'outputs must be listed even though the unlock failed');
	assert.equal(hp.inputs.length, 1, 'inputs must be listed even though the unlock failed');
	assert.match(hp.error ?? '', /acquisition failed: .*Permission denied/);
});

test('if the label unlock succeeds then the I/O menu lists the labelled devices', async () => {
	installFakeBrowser({ getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) });
	await headphones.acquireHeadphoneOutput(monitorSource);
	const hp = playerState.mixerState.headphones;
	assert.deepEqual(
		hp.outputs.map((output) => output.label),
		// IOPIN-14: the system default is always listed, first, when the browser names none.
		['System default output', 'MacBook Pro Speakers', "Steve's over-ears"]
	);
	assert.equal(hp.error, null);
});
