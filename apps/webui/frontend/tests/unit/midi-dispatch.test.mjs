// End-to-end dispatch tests for the P0 midi pipeline WITHOUT hardware:
// synthetic [status, d1, d2] byte triples are pushed through the REAL
// initMidi() -> _rescanPorts -> _dispatch path by patching
// navigator.requestMIDIAccess with a fake MIDIAccess (fake input/output
// ports). Nothing in src/ is stubbed - the registered device maps
// (FLX10 + Mixtour) and the full binding/pairing/learn-log logic run
// exactly as they would in Chrome.
//
// Task scenarios covered (controller P0 integration step 2):
//   FLX10 play note        -> deck_play_toggle action
//   FLX10 pad note         -> deck_hot_cue action w/ correct slot
//   FLX10 14-bit tempo pair-> ONE deck_pitch emit w/ combined value
//   unmapped message       -> learn-log entry (never silent)
//   Mixtour fader CC       -> mixer_channel fader action
//   LED rule               -> correct outbound [0x9n, note, vel] bytes
//
// Regression lines (single-line format per CLAUDE.md):
//   if [0x90,0x0B,0x7F] on FLX10 isn't deck_play_toggle deck 1 then broken
//   if [0x9B,0x03,0x7F] isn't deck_hot_cue deck 3 slot D then broken
//   if MSB 0x40 + LSB 0x00 on ch2 CC0/32 isn't ONE raw-8192 pitch then broken
//   if an unmapped note doesn't land in learnLog as mapped:false then broken
//   if [0xB0,0x05,64] on Mixtour isn't deck 1 fader 64/127 then broken
//   if a queued LED write doesn't flush as [0x90|ch-1,note,vel] then broken
//   if two LED writes to one (ch,note) within a window send twice then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi; // $lib/rb/midi/webmidi.svelte.ts
let flx10; // $lib/rb/midi/maps/ddj-flx10.ts
let mixtour; // $lib/rb/midi/maps/reloop-mixtour.ts

// ------------------------------------------------------- fake WebMIDI ports

function _fakeInput(id, name, manufacturer) {
	return { id, name, manufacturer, state: 'connected', onmidimessage: null };
}

function _fakeOutput(id, name) {
	return {
		id,
		name,
		state: 'connected',
		sent: [],
		send(bytes) {
			this.sent.push([...bytes]);
		}
	};
}

const flxIn = _fakeInput('flx-in', 'DDJ-FLX10', 'AlphaTheta');
const flxOut = _fakeOutput('flx-out', 'DDJ-FLX10');
const mixIn = _fakeInput('mix-in', 'Mixtour', 'Reloop');

const fakeAccess = {
	inputs: new Map([
		[flxIn.id, flxIn],
		[mixIn.id, mixIn]
	]),
	outputs: new Map([[flxOut.id, flxOut]]),
	onstatechange: null
};

/** Push one raw wire message into the dispatcher, exactly as Chrome would. */
function wire(input, status, d1, d2) {
	assert.notEqual(input.onmidimessage, null, `${input.name} got no onmidimessage listener`);
	input.onmidimessage({ data: Uint8Array.of(status, d1, d2) });
}

// Captured (action, value, deviceId) emissions from the real dispatcher.
const actions = [];
let requestedOptions = null;

// ----------------------------------------------------------------- lifecycle

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	flx10 = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx10.ts');
	mixtour = await vite.ssrLoadModule('/src/lib/rb/midi/maps/reloop-mixtour.ts');

	// Patch navigator.requestMIDIAccess (Node 22 ships a navigator object;
	// fall back to redefining the global if it rejects new properties).
	const request = async (options) => {
		requestedOptions = options;
		return fakeAccess;
	};
	try {
		globalThis.navigator.requestMIDIAccess = request;
	} catch {
		Object.defineProperty(globalThis, 'navigator', {
			value: { requestMIDIAccess: request },
			configurable: true
		});
	}

	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(flx10.FLX10_MAP);
	webmidi.registerDeviceMap(mixtour.RELOOP_MIXTOUR_MAP);
	webmidi.registerActionHandler((action, value, deviceId) => {
		actions.push({ action, value, deviceId });
	});
	await webmidi.initMidi();
});

after(async () => {
	webmidi._resetMidiForTests();
	await vite.close();
});

// ------------------------------------------------------------ device resolve

test('initMidi requested sysex:false and resolved both fake devices to maps', () => {
	assert.deepEqual(requestedOptions, { sysex: false });
	assert.equal(webmidi.midiState.permission, 'granted');
	const byId = new Map(webmidi.midiState.devices.map((d) => [d.id, d]));
	assert.equal(byId.get('flx-in').mapVendor, 'Pioneer DJ');
	assert.equal(byId.get('flx-in').hasOutput, true);
	assert.equal(byId.get('mix-in').mapVendor, 'Reloop');
	assert.equal(byId.get('mix-in').hasOutput, false);
});

// ------------------------------------------------------------ FLX10 inbound

test('FLX10 play note -> deck_play_toggle action', () => {
	const n = actions.length;
	// [PDF] D1 PLAY/PAUSE: Note On ch 1 (status 0x90), note 11 (0x0B).
	wire(flxIn, 0x90, 0x0b, 0x7f);
	assert.equal(actions.length, n + 1);
	assert.deepEqual(actions[n].action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(actions[n].value, { kind: 'button', pressed: true, velocity: 0x7f });
	assert.equal(actions[n].deviceId, 'flx-in');
	assert.equal(webmidi.learnLog[0].mapped, true);
	// The learn-log entry carries the matched action so the UI can render a
	// friendly label ('Play (deck 1)') instead of the bare type string.
	assert.deepEqual(webmidi.learnLog[0].action, { type: 'deck_play_toggle', deck: 1 });
});

test('FLX10 pad note -> deck_hot_cue action with the correct slot', () => {
	const n = actions.length;
	// Deck 3 pads live on ch 12 (status 0x9B); HOT CUE PAGE1 pad 4 sends
	// note 3 -> slot D ([PDF] p.4-7 P1..P8 + channel table).
	wire(flxIn, 0x9b, 0x03, 0x7f);
	assert.equal(actions.length, n + 1);
	assert.deepEqual(actions[n].action, { type: 'deck_hot_cue', deck: 3, slot: 'D' });
	assert.equal(actions[n].value.pressed, true);
});

test('FLX10 14-bit tempo pair -> ONE deck_pitch action with combined value', () => {
	const n = actions.length;
	// [PDF] D4 TEMPO deck 2: ch 2 (status 0xB1), CC 0 MSB / CC 32 LSB.
	wire(flxIn, 0xb1, 0x00, 0x40); // MSB 0x40: stored, MUST NOT emit yet
	assert.equal(actions.length, n);
	assert.match(webmidi.learnLog[0].note, /MSB stored/);
	wire(flxIn, 0xb1, 0x20, 0x00); // LSB 0x00 completes the pair
	assert.equal(actions.length, n + 1);
	assert.deepEqual(actions[n].action, { type: 'deck_pitch', deck: 2, lsbOffset: 32 });
	assert.equal(actions[n].value.kind, 'continuous14');
	assert.equal(actions[n].value.raw, 8192); // 0x40 << 7 | 0x00
	assert.ok(Math.abs(actions[n].value.value01 - 8192 / 16383) < 1e-9);
});

// --------------------------------------------------------- learn-log capture

test('unmapped message -> learn-log entry, no action, never silent', () => {
	const n = actions.length;
	// Note 10 (0x0A) ch 1 is deliberately unbound in the FLX10 P0 map.
	wire(flxIn, 0x90, 0x0a, 0x7f);
	assert.equal(actions.length, n);
	const entry = webmidi.learnLog[0];
	assert.equal(entry.mapped, false);
	assert.equal(entry.note, 'unmapped source');
	assert.equal(entry.status, 0x90);
	assert.equal(entry.data1, 0x0a);
	assert.deepEqual(entry.decoded, { ch: 1, kind: 'note', id: 0x0a });
	// No binding matched -> no action attached (UI falls back to a best-guess
	// hint or the raw note, never a phantom action label).
	assert.equal(entry.action ?? null, null);
});

test('undecoded status family -> learn-log entry flagged out of P0 scope', () => {
	const n = actions.length;
	wire(flxIn, 0xc0, 0x05, 0x00); // program change: outside P0 decode
	assert.equal(actions.length, n);
	assert.equal(webmidi.learnLog[0].mapped, false);
	assert.match(webmidi.learnLog[0].note, /out of P0 scope/);
});

// ----------------------------------------------------------- Mixtour inbound

test('Mixtour fader CC -> mixer_channel fader action', () => {
	const n = actions.length;
	// [S4] deck 1 channel fader: ch 1 (status 0xB0), CC 5 (0x05).
	wire(mixIn, 0xb0, 0x05, 64);
	assert.equal(actions.length, n + 1);
	assert.deepEqual(actions[n].action, { type: 'mixer_channel', deck: 1, target: 'fader' });
	assert.equal(actions[n].value.kind, 'continuous');
	assert.equal(actions[n].value.raw, 64);
	assert.ok(Math.abs(actions[n].value.value01 - 64 / 127) < 1e-9);
	assert.equal(actions[n].deviceId, 'mix-in');
});

test('Mixtour pitch bend -> single-message 14-bit deck_pitch', () => {
	const n = actions.length;
	// [S4] deck 1 rate: Pitch Bend ch 1 (status 0xE0), d1=LSB d2=MSB.
	wire(mixIn, 0xe0, 0x00, 0x40);
	assert.equal(actions.length, n + 1);
	assert.equal(actions[n].action.type, 'deck_pitch');
	assert.equal(actions[n].action.deck, 1);
	assert.equal(actions[n].value.kind, 'continuous14');
	assert.equal(actions[n].value.raw, 8192);
});

// -------------------------------------------------------------- LED outbound

test('LED rule -> correct outbound Note On bytes on the device output', async () => {
	// Use the REAL FLX10 LedRule for deck 1 playing ([PDF] D1 MIDI-OUT).
	const rule = flx10.FLX10_MAP.leds.find(
		(r) => r.trigger.kind === 'deck_playing' && r.trigger.deck === 1
	);
	assert.notEqual(rule, undefined);
	flxOut.sent.length = 0;
	webmidi.sendLed('flx-in', rule.out.ch, rule.out.note, rule.out.velocityOn);
	await sleep(webmidi.LED_THROTTLE_MS * 3); // let the throttle timer flush
	assert.deepEqual(flxOut.sent, [[0x90, 0x0b, 0x7f]]); // 0x90 = Note On ch 1
});

test('LED writes to one (ch,note) coalesce within a flush window: last wins', async () => {
	const rule = flx10.FLX10_MAP.leds.find(
		(r) => r.trigger.kind === 'deck_playing' && r.trigger.deck === 1
	);
	flxOut.sent.length = 0;
	webmidi.sendLed('flx-in', rule.out.ch, rule.out.note, rule.out.velocityOn);
	webmidi.sendLed('flx-in', rule.out.ch, rule.out.note, rule.out.velocityOff);
	await sleep(webmidi.LED_THROTTLE_MS * 3);
	assert.deepEqual(flxOut.sent, [[0x90, 0x0b, 0x00]]);
});

test('sendLed to a device without an output port fails fast', () => {
	assert.throws(() => webmidi.sendLed('mix-in', 1, 0x0b, 0x7f), /no MIDI output/);
});
