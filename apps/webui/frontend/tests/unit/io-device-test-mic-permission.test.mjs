// requirement: IOPIN-19 (Audio I/O panel Test buttons and level meters)
// [if] the browser cannot answer the microphone permission query [then] readable input labels still say it is granted
// [if] no input label is readable and the query cannot answer [then] the meter does not open on its own
// [if] the query answers [then] its answer stands (control)
// [if] this page opened the microphone this session and the query cannot answer [then] the meter may open again without asking
// [if] this page opened the microphone this session but the query now answers it is not granted (access revoked) [then] the meter does not open on its own
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// A fresh module: the session-grant memory must start empty here.
const io = await loadTypeScriptModule('src/lib/player/io-device-test-audio.ts');

async function withNavigator(value, run) {
	const saved = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	Object.defineProperty(globalThis, 'navigator', { configurable: true, value });
	try {
		await run();
	} finally {
		if (saved) Object.defineProperty(globalThis, 'navigator', saved);
		else delete globalThis.navigator;
	}
}

const devices = (label) => async () => [
	{ kind: 'audiooutput', label: 'Speakers' },
	{ kind: 'audioinput', label }
];

test('with no permission query, readable input labels count as granted and blank ones do not', async () => {
	await withNavigator({ mediaDevices: { enumerateDevices: devices('MacBook Pro Microphone') } }, async () => {
		assert.equal(await io.microphonePermissionGranted(), true);
	});
	await withNavigator({ mediaDevices: { enumerateDevices: devices('') } }, async () => {
		assert.equal(await io.microphonePermissionGranted(), false);
	});
});

test('a rejected permission query falls back to the input labels', async () => {
	const permissions = { query: async () => Promise.reject(new TypeError("'microphone' is not a valid PermissionName")) };
	await withNavigator({ permissions, mediaDevices: { enumerateDevices: devices('USB Mic') } }, async () => {
		assert.equal(await io.microphonePermissionGranted(), true);
	});
});

test('control: an answering permission query decides, whatever the labels say', async () => {
	for (const [state, want] of [
		['granted', true],
		['prompt', false],
		['denied', false]
	]) {
		const permissions = { query: async () => ({ state }) };
		await withNavigator({ permissions, mediaDevices: { enumerateDevices: devices('USB Mic') } }, async () => {
			assert.equal(await io.microphonePermissionGranted(), want, state);
		});
	}
});

test('a microphone opened this session counts as granted even where nothing can be queried', async () => {
	const stream = { getTracks: () => [{ stop() {} }] };
	const savedCtx = globalThis.AudioContext;
	globalThis.AudioContext = class {
		state = 'running';
		createMediaStreamSource() {
			return { connect() {}, disconnect() {} };
		}
		createAnalyser() {
			return { fftSize: 0, getFloatTimeDomainData() {} };
		}
		async close() {}
	};
	const savedRaf = globalThis.requestAnimationFrame;
	const savedCancel = globalThis.cancelAnimationFrame;
	globalThis.requestAnimationFrame = () => 1;
	globalThis.cancelAnimationFrame = () => {};
	try {
		await withNavigator({ mediaDevices: { getUserMedia: async () => stream, enumerateDevices: devices('') } }, async () => {
			assert.equal(await io.microphonePermissionGranted(), false, 'precondition: nothing says granted yet');
			const stop = await io.startInputLevelWatch('mic', () => {});
			stop();
			assert.equal(await io.microphonePermissionGranted(), true);
		});
		// Access revoked since: a query that answers is current, so the session's
		// open never stands in for it (Sol P1 on #6835).
		for (const state of ['prompt', 'denied']) {
			const permissions = { query: async () => ({ state }) };
			await withNavigator({ permissions, mediaDevices: { enumerateDevices: devices('') } }, async () => {
				assert.equal(await io.microphonePermissionGranted(), false, state);
			});
		}
		// Control: a query that cannot answer still leaves the session's open standing.
		const rejecting = { query: async () => Promise.reject(new TypeError('no microphone descriptor')) };
		await withNavigator({ permissions: rejecting, mediaDevices: { enumerateDevices: devices('') } }, async () => {
			assert.equal(await io.microphonePermissionGranted(), true);
		});
	} finally {
		globalThis.AudioContext = savedCtx;
		globalThis.requestAnimationFrame = savedRaf;
		globalThis.cancelAnimationFrame = savedCancel;
	}
});
