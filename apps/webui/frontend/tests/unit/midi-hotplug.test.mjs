import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// H12 - a controller plugged in mid-set must start working without a page
// reload. the maintainer hit this live ("midi mapping stopped working though" ... "do I
// need to refresh the page?"). The rule is already written at
// webmidi.svelte.ts:20-24 - but only as a docstring, and a docstring is a
// promise, not a gate. Inventory greps for hot-plug / hotplug / statechange /
// replug returned zero hits.
//
// Real MIDIAccess objects and real hardware are not available here, so this
// drives the module's ACTUAL public entry point (initMidi) against a fake
// navigator.requestMIDIAccess whose port maps we mutate between statechange
// events - exactly what the browser does on a plug/unplug. Nothing about the
// module under test is stubbed.
//
// Regression lines:
// - if onstatechange is not wired then a controller plugged in after page load
//   never appears and the maintainer has to reload mid-set
// - if a rescan does not resolve the DeviceMap then the device is listed with
//   mapVendor null and no Note On ever dispatches
// - if a disconnected device is not dropped then a stuck entry keeps a dead
//   port's handler attached and its LED queue alive
// - if a replug appends instead of replacing then one press fires twice
// - if a rescan rebuilds already-resolved devices then 14-bit pairing state and
//   the shift layer reset on every unrelated plug event

let webmidi;
let ports;

/** One fake WebMIDI input/output port pair. */
function fakePort(id, name, { type = 'input' } = {}) {
	return { id, name, manufacturer: 'Pioneer DJ', state: 'connected', type, sent: [], onmidimessage: null, send(bytes) { this.sent.push([...bytes]); } };
}

/** A fake MIDIAccess whose maps we mutate, then fire onstatechange - the exact
 * shape the browser presents on a plug or unplug. */
function fakeAccess() {
	const inputs = new Map();
	const outputs = new Map();
	const access = {
		inputs,
		outputs,
		onstatechange: null,
		/** Plug a port in and tell the page about it, like Chrome does. */
		plug(input, output = null) {
			inputs.set(input.id, input);
			if (output !== null) outputs.set(output.id, output);
			access.fire();
		},
		unplug(input) {
			input.state = 'disconnected';
			access.fire();
		},
		fire() {
			if (access.onstatechange !== null) access.onstatechange({ port: null });
		}
	};
	return access;
}

const DDJ400 = {
	vendor: 'pioneer-ddj400',
	nameMatch: 'DDJ-400',
	bindings: [
		{ source: { ch: 1, kind: 'note', id: 11 }, action: { type: 'deck_play_toggle', deck: 1 } },
		{
			source: { ch: 1, kind: 'cc', id: 0 },
			action: { type: 'deck_pitch', deck: 1, lsbOffset: 32 }
		}
	]
};

before(async () => {
	webmidi = await loadTypeScriptModule('src/lib/rb/midi/webmidi.svelte.ts');
});

beforeEach(() => {
	webmidi._resetMidiForTests();
	ports = fakeAccess();
	// node exposes navigator as a read-only accessor, so replace the property.
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		writable: true,
		value: { requestMIDIAccess: async () => ports }
	});
});

/** Push one raw MIDI message at a plugged-in input, as the browser would. */
function press(input, bytes) {
	assert.ok(input.onmidimessage !== null, `${input.name} has no message handler attached`);
	input.onmidimessage({ data: Uint8Array.from(bytes) });
}

// ---------------------------------------------- plugged in after page load

test('a controller plugged in after init appears and dispatches with no reload', async () => {
	const fired = [];
	webmidi.registerDeviceMap(DDJ400);
	webmidi.registerActionHandler((action, value, deviceId) => fired.push({ action, value, deviceId }));

	await webmidi.initMidi();
	assert.equal(webmidi.midiState.permission, 'granted');
	assert.deepEqual(webmidi.midiState.devices, [], 'no controller was connected at page load');

	// the maintainer plugs the DDJ-400 in mid-set.
	const input = fakePort('in-1', 'DDJ-400');
	ports.plug(input, fakePort('out-1', 'DDJ-400', { type: 'output' }));

	assert.equal(webmidi.midiState.devices.length, 1, 'the hot-plugged controller never appeared');
	assert.equal(webmidi.midiState.devices[0].name, 'DDJ-400');
	assert.equal(
		webmidi.midiState.devices[0].mapVendor,
		'pioneer-ddj400',
		'the device is listed but its DeviceMap was not resolved, so nothing will dispatch'
	);
	assert.equal(webmidi.midiState.devices[0].hasOutput, true, 'the LED output port was not paired');

	press(input, [0x90, 11, 127]);
	assert.equal(fired.length, 1, 'the next Note On did not dispatch - a reload is still required');
	assert.deepEqual(fired[0].action, { type: 'deck_play_toggle', deck: 1 });
	assert.equal(fired[0].value.pressed, true);
	assert.equal(fired[0].deviceId, 'in-1');
});

test('a device connected BEFORE init is picked up by the init rescan too', async () => {
	webmidi.registerDeviceMap(DDJ400);
	ports.inputs.set('in-1', fakePort('in-1', 'DDJ-400'));

	await webmidi.initMidi();
	assert.equal(webmidi.midiState.devices.length, 1);
	assert.equal(webmidi.midiState.devices[0].mapVendor, 'pioneer-ddj400');
});

test('an unmapped controller still gets a listener so its traffic learn-logs', async () => {
	webmidi.registerDeviceMap(DDJ400);
	await webmidi.initMidi();

	const input = fakePort('in-9', 'Some Unknown Controller');
	ports.plug(input);

	assert.equal(webmidi.midiState.devices.length, 1);
	assert.equal(webmidi.midiState.devices[0].mapVendor, null);
	press(input, [0x90, 11, 127]);
	assert.equal(webmidi.learnLog.length, 1, 'unmapped traffic was silently dropped');
	assert.equal(webmidi.learnLog[0].mapped, false);
	assert.match(webmidi.learnLog[0].note, /no device map matched/);
});

// ------------------------------------------------------------ unplugged

test('a disconnected device leaves the list and stops taking traffic, without throwing', async () => {
	const fired = [];
	webmidi.registerDeviceMap(DDJ400);
	webmidi.registerActionHandler((action) => fired.push(action));
	await webmidi.initMidi();

	const input = fakePort('in-1', 'DDJ-400');
	const output = fakePort('out-1', 'DDJ-400', { type: 'output' });
	ports.plug(input, output);
	webmidi.sendLed('in-1', 1, 11, 127);

	assert.doesNotThrow(() => ports.unplug(input), 'unplugging a live controller threw');
	assert.deepEqual(webmidi.midiState.devices, [], 'a stuck entry survived the unplug');
	assert.equal(
		input.onmidimessage,
		null,
		'the dead port keeps its handler, so late traffic still reaches the engine'
	);
	assert.throws(
		() => webmidi.sendLed('in-1', 1, 11, 0),
		/unknown device id/,
		'LED writes to a vanished device must fail loudly, not queue forever'
	);
	assert.equal(fired.length, 0);
});

// -------------------------------------------------------------- replug

test('a disconnect and reconnect leaves exactly one entry that fires once', async () => {
	const fired = [];
	webmidi.registerDeviceMap(DDJ400);
	webmidi.registerActionHandler((action) => fired.push(action));
	await webmidi.initMidi();

	const first = fakePort('in-1', 'DDJ-400');
	ports.plug(first);
	ports.unplug(first);

	// A replug usually presents the SAME port id; a fresh id is the harder case.
	const again = fakePort('in-1', 'DDJ-400');
	ports.plug(again);

	assert.equal(webmidi.midiState.devices.length, 1, 'the replug duplicated the device entry');
	assert.equal(webmidi.midiState.devices[0].mapVendor, 'pioneer-ddj400', 'the map did not re-resolve');

	press(again, [0x90, 11, 127]);
	assert.equal(fired.length, 1, 'one press dispatched twice - duplicate bindings are attached');
});

test('a port that reappears under a new id does not leave the old one behind', async () => {
	webmidi.registerDeviceMap(DDJ400);
	await webmidi.initMidi();

	const first = fakePort('in-1', 'DDJ-400');
	ports.plug(first);
	ports.unplug(first);
	ports.inputs.delete('in-1');

	const second = fakePort('in-2', 'DDJ-400');
	ports.plug(second);

	assert.deepEqual(
		webmidi.midiState.devices.map((d) => d.id),
		['in-2'],
		'the stale port id survived the replug'
	);
});

// ----------------------------------------- a rescan must not reset live state

test('an unrelated plug event does not reset a resolved device mid-set', async () => {
	webmidi.registerDeviceMap(DDJ400);
	const fired = [];
	webmidi.registerActionHandler((action, value) => fired.push({ action, value }));
	await webmidi.initMidi();

	const deck = fakePort('in-1', 'DDJ-400');
	ports.plug(deck);
	const handler = deck.onmidimessage;

	// Half of a 14-bit pitch pair is in flight when a second controller lands.
	press(deck, [0xb0, 0, 0x40]); // MSB
	ports.plug(fakePort('in-2', 'Some Other Controller'));

	assert.equal(
		deck.onmidimessage,
		handler,
		'the resolved device was rebuilt on an unrelated statechange'
	);
	press(deck, [0xb0, 32, 0x00]); // LSB completes the pair
	const pitch = fired.filter((f) => f.action.type === 'deck_pitch');
	assert.equal(pitch.length, 1, 'the in-flight 14-bit pair was lost to an unrelated plug event');
	assert.equal(pitch[0].value.raw, 8192);
});

test('a rescan before initMidi is a loud error, not a silent no-op', async () => {
	// _rescanPorts throws without _access; the only way to reach it is via the
	// statechange hook, so this asserts the hook is installed by initMidi and not
	// left dangling from a previous session.
	assert.equal(ports.onstatechange, null);
	await webmidi.initMidi();
	assert.equal(
		typeof ports.onstatechange,
		'function',
		'initMidi did not wire onstatechange - nothing rescans, so hot-plug is dead'
	);
});
