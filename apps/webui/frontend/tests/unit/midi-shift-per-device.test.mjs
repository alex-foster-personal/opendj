// Per-device MIDI shift reset-path regression (issue #13311).
// Proves selective disconnect cleanup and _resetMidiForTests cannot leak
// shift state across controllers. Behavior ships as CTRL-10544; dispatch
// tests in midi-dispatch.test.mjs still cite the same feature.
//
// Regression lines:
//   if two controllers hold shift independently and B fires unshifted then broken
//   if disconnecting one device while both hold shift leaves the other stuck then broken
//   if disconnecting the only shift holder leaves aggregate shiftHeld true then broken
//   if _resetMidiForTests leaves any shiftHeldByDevice entry then broken

import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi;

function _fakeInput(id, name, manufacturer) {
	return { id, name, manufacturer, state: 'connected', onmidimessage: null };
}

const SHIFT_TEST_MAP = {
	vendor: 'shift-test',
	nameMatch: '^ShiftTest Box$',
	bindings: [
		{ source: { ch: 1, kind: 'note', id: 0x3f }, action: { type: 'shift_modifier' } },
		{ source: { ch: 1, kind: 'note', id: 0x10 }, action: { type: 'deck_play_toggle', deck: 1 } },
		{
			source: { ch: 1, kind: 'note', id: 0x10 },
			shift: true,
			action: { type: 'deck_cue', deck: 1 }
		}
	]
};

const shiftAIn = _fakeInput('shift-a', 'ShiftTest Box', 'Test');
const shiftBIn = _fakeInput('shift-b', 'ShiftTest Box', 'Test');

const fakeAccess = {
	inputs: new Map(),
	outputs: new Map(),
	onstatechange: null
};

/** Push one raw wire message into the dispatcher, exactly as Chrome would. */
function wire(input, status, d1, d2) {
	assert.notEqual(input.onmidimessage, null, `${input.name} got no onmidimessage listener`);
	input.onmidimessage({ data: Uint8Array.of(status, d1, d2) });
}

const actions = [];
let shiftMapRegistered = false;

function _connectShiftBoxes() {
	for (const input of [shiftAIn, shiftBIn]) {
		input.state = 'connected';
		fakeAccess.inputs.set(input.id, input);
	}
	if (!shiftMapRegistered) webmidi.registerDeviceMap(SHIFT_TEST_MAP);
	shiftMapRegistered = true;
	fakeAccess.onstatechange();
}

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');

	const request = async () => fakeAccess;
	try {
		globalThis.navigator.requestMIDIAccess = request;
	} catch {
		Object.defineProperty(globalThis, 'navigator', {
			value: { requestMIDIAccess: request },
			configurable: true
		});
	}

	webmidi._resetMidiForTests();
	webmidi.registerActionHandler((action, value, deviceId) => {
		actions.push({ action, value, deviceId });
	});
	await webmidi.initMidi();
});

beforeEach(() => {
	if (!webmidi) return;
	for (const input of [shiftAIn, shiftBIn]) {
		input.state = 'connected';
		fakeAccess.inputs.set(input.id, input);
	}
	webmidi.midiState.shiftHeldByDevice = {};
	webmidi.midiState.shiftHeld = false;
	actions.length = 0;
});

after(async () => {
	try {
		webmidi?._resetMidiForTests();
	} finally {
		await vite?.close();
	}
});

test('two devices hold shift independently for dispatch', () => {
	_connectShiftBoxes();
	wire(shiftAIn, 0x90, 0x3f, 0x7f);
	assert.equal(webmidi.midiState.shiftHeldByDevice['shift-a'], true);
	assert.equal(webmidi.midiState.shiftHeldByDevice['shift-b'], undefined);
	assert.equal(webmidi.midiState.shiftHeld, true);

	const n = actions.length;
	wire(shiftBIn, 0x90, 0x10, 0x7f);
	assert.deepEqual(actions[n].action, { type: 'deck_play_toggle', deck: 1 });
	assert.equal(actions[n].deviceId, 'shift-b');

	wire(shiftAIn, 0x90, 0x10, 0x7f);
	assert.deepEqual(actions[n + 1].action, { type: 'deck_cue', deck: 1 });
	assert.equal(actions[n + 1].deviceId, 'shift-a');

	wire(shiftAIn, 0x80, 0x3f, 0x00);
	assert.equal(webmidi.midiState.shiftHeldByDevice['shift-a'], undefined);
	assert.equal(webmidi.midiState.shiftHeld, false);
});

test('disconnecting one device clears only its shift layer when the other still holds shift', () => {
	_connectShiftBoxes();
	wire(shiftAIn, 0x90, 0x3f, 0x7f);
	wire(shiftBIn, 0x90, 0x3f, 0x7f);
	assert.deepEqual(webmidi.midiState.shiftHeldByDevice, { 'shift-a': true, 'shift-b': true });
	assert.equal(webmidi.midiState.shiftHeld, true);

	shiftAIn.state = 'disconnected';
	fakeAccess.inputs.delete(shiftAIn.id);
	fakeAccess.onstatechange();

	assert.equal(webmidi.midiState.shiftHeldByDevice['shift-a'], undefined);
	assert.equal(webmidi.midiState.shiftHeldByDevice['shift-b'], true);
	assert.equal(webmidi.midiState.shiftHeld, true);

	const n = actions.length;
	wire(shiftBIn, 0x90, 0x10, 0x7f);
	assert.deepEqual(actions[n].action, { type: 'deck_cue', deck: 1 });
	assert.equal(actions[n].deviceId, 'shift-b');
});

test('disconnecting the only shift holder clears aggregate shiftHeld', () => {
	_connectShiftBoxes();
	wire(shiftAIn, 0x90, 0x3f, 0x7f);
	assert.equal(webmidi.midiState.shiftHeld, true);

	shiftAIn.state = 'disconnected';
	fakeAccess.inputs.delete(shiftAIn.id);
	fakeAccess.onstatechange();

	assert.equal(webmidi.midiState.shiftHeldByDevice['shift-a'], undefined);
	assert.equal(webmidi.midiState.shiftHeld, false);
});

// Keep last: full test reset tears down runtime listeners like dispatch's release test.
test('_resetMidiForTests clears every device shift entry', () => {
	_connectShiftBoxes();
	wire(shiftAIn, 0x90, 0x3f, 0x7f);
	wire(shiftBIn, 0x90, 0x3f, 0x7f);
	assert.equal(Object.keys(webmidi.midiState.shiftHeldByDevice).length, 2);

	webmidi._resetMidiForTests();
	assert.deepEqual(webmidi.midiState.shiftHeldByDevice, {});
	assert.equal(webmidi.midiState.shiftHeld, false);
});
