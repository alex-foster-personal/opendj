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
//   installed map (#10058) -> init triples once on resolve, again on hot-plug,
//                             never on a plain rescan; deck_playing LED lights
//
// Regression lines (single-line format per CLAUDE.md):
//   if [0x90,0x0B,0x7F] on FLX10 isn't deck_play_toggle deck 1 then broken
//   if [0x9B,0x03,0x7F] isn't deck_hot_cue deck 3 slot D then broken
//   if MSB 0x40 + LSB 0x00 on ch2 CC0/32 isn't ONE raw-8192 pitch then broken
//   if an unmapped note doesn't land in learnLog as mapped:false then broken
//   if [0xB0,0x05,64] on Mixtour isn't deck 1 fader 64/127 then broken
//   if a queued LED write doesn't flush as [0x90|ch-1,note,vel] then broken
//   if two LED writes to one (ch,note) within a window send twice then broken
//   if an installed map's init triples are not sent in order on resolve then broken
//   if a plain rescan of a resolved port resends init then broken
//   if a hot-plug reconnect does not resend init then broken
//   if an installed deck_playing LED rule does not light its pad then broken

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
let mixtourPro; // $lib/rb/midi/maps/reloop-mixtour-pro.ts
let ddj400; // $lib/rb/midi/maps/ddj-400.ts
let flx4; // $lib/rb/midi/maps/ddj-flx4.ts
let installed; // $lib/rb/midi/installed-maps.ts
let glue; // $lib/rb/midi/action-glue.svelte.ts
let audioEngine; // $lib/rb/audio-engine.svelte.ts
let padRuntime; // $lib/rb/midi/controller-pad-runtime.svelte.ts
let initSentAtConnect = [];

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
const mixProIn = _fakeInput('mix-pro-in', 'Reloop Mixtour Pro', 'Reloop');
const mixProOut = _fakeOutput('mix-pro-out', 'Reloop Mixtour Pro');
const ddjIn = _fakeInput('ddj-in', 'DDJ-400', 'Pioneer DJ');
const ddjOut = _fakeOutput('ddj-out', 'DDJ-400');
const flx4In = _fakeInput('flx4-in', 'DDJ-FLX4', 'Pioneer DJ');
const flx4Out = _fakeOutput('flx4-out', 'DDJ-FLX4');
const instIn = _fakeInput('inst-in', 'OpenDJ Installed Pad', 'OpenDJ');
const instOut = _fakeOutput('inst-out', 'OpenDJ Installed Pad');

// Fixture numbers only, not claimed hardware numbers.
const _CITE = { tier: 'hand', cite: 'midi-dispatch fixture', verified: false };
const INIT_TRIPLES = [
	[0xb0, 0x63, 0x01],
	[0xb0, 0x64, 0x00]
];
const INSTALLED_DOC = {
	schemaVersion: 1,
	id: 'opendj-installed-pad',
	vendor: 'OpenDJ Installed',
	model: 'Installed Pad',
	nameMatch: 'OpenDJ Installed Pad',
	bindings: [
		{
			source: { ch: 1, kind: 'note', id: 0x20 },
			action: { type: 'deck_play_toggle', deck: 1 },
			provenance: _CITE
		}
	],
	leds: [
		{
			trigger: { kind: 'deck_playing', deck: 1 },
			padMode: null,
			out: { ch: 1, note: 0x30, velocityOn: 0x7f, velocityOff: 0 },
			provenance: _CITE
		}
	],
	meters: [],
	init: INIT_TRIPLES.map((message) => ({ message, provenance: _CITE }))
};

const fakeAccess = {
	inputs: new Map([
		[flxIn.id, flxIn],
		[mixIn.id, mixIn],
		[mixProIn.id, mixProIn],
		[ddjIn.id, ddjIn],
		[flx4In.id, flx4In],
		[instIn.id, instIn]
	]),
	outputs: new Map([
		[flxOut.id, flxOut],
		[mixProOut.id, mixProOut],
		[ddjOut.id, ddjOut],
		[flx4Out.id, flx4Out],
		[instOut.id, instOut]
	]),
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
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	flx10 = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx10.ts');
	mixtour = await vite.ssrLoadModule('/src/lib/rb/midi/maps/reloop-mixtour.ts');
	mixtourPro = await vite.ssrLoadModule('/src/lib/rb/midi/maps/reloop-mixtour-pro.ts');
	ddj400 = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-400.ts');
	flx4 = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx4.ts');
	installed = await vite.ssrLoadModule('/src/lib/rb/midi/installed-maps.ts');
	glue = await vite.ssrLoadModule('/src/lib/rb/midi/action-glue.svelte.ts');
	audioEngine = await vite.ssrLoadModule('/src/lib/rb/audio-engine.svelte.ts');
	padRuntime = await vite.ssrLoadModule('/src/lib/rb/midi/controller-pad-runtime.svelte.ts');

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
	webmidi.registerDeviceMap(mixtourPro.RELOOP_MIXTOUR_PRO_MAP);
	webmidi.registerDeviceMap(ddj400.DDJ400_MAP);
	webmidi.registerDeviceMap(flx4.FLX4_MAP);
	webmidi.registerDeviceMap(installed.deviceMapFromDoc(INSTALLED_DOC), 'installed');
	webmidi.registerActionHandler((action, value, deviceId) => {
		actions.push({ action, value, deviceId });
	});
	await webmidi.initMidi();
	initSentAtConnect = [...instOut.sent];
});

after(async () => {
	try {
		webmidi?._resetMidiForTests();
	} finally {
		// Setup can fail before webmidi is assigned. Always close a Vite server
		// that was created so Node does not retain its listeners indefinitely.
		await vite?.close();
	}
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
	assert.equal(byId.get('mix-pro-in').mapVendor, 'Reloop');
	assert.equal(byId.get('mix-pro-in').hasOutput, true);
	assert.equal(byId.get('ddj-in').mapVendor, 'Pioneer DJ');
	assert.equal(byId.get('ddj-in').hasOutput, true);
	assert.equal(byId.get('flx4-in').mapVendor, 'Pioneer DJ');
	assert.equal(byId.get('flx4-in').hasOutput, true);
	const flx4Map = webmidi.getDeviceMap('flx4-in');
	assert.notEqual(flx4Map, null);
	assert.equal(flx4Map.bindings.length, flx4.FLX4_MAP.bindings.length);
	assert.ok(flx4Map.bindings.length > 0);
	assert.equal(byId.get('inst-in').mapVendor, 'OpenDJ Installed');
	assert.equal(byId.get('inst-in').hasOutput, true);
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

test('FLX10 cue-surface wire numbers stay unmapped (per-map, never global)', () => {
	const n = actions.length;
	wire(flxIn, 0xb6, 0x0c, 64); // ch 7 CC 12 MIXING
	wire(flxIn, 0xb6, 0x0d, 64); // ch 7 CC 13 LEVEL
	wire(flxIn, 0x90, 0x54, 0x7f); // ch 1 note 0x54 CH CUE
	wire(flxIn, 0x96, 0x63, 0x7f); // ch 7 note 0x63 MASTER CUE
	assert.equal(actions.length, n);
	for (const entry of webmidi.learnLog.slice(0, 4)) {
		assert.equal(entry.mapped, false);
	}
});

test('Mixtour PFL note 0x03 -> channel_cue deck 1', () => {
	const n = actions.length;
	wire(mixIn, 0x90, 0x03, 0x7f);
	assert.equal(actions.length, n + 1);
	assert.deepEqual(actions[n].action, { type: 'channel_cue', deck: 1 });
});

test('Mixtour Pro messages resolve to the Pro map and normalize browse to one row', () => {
	let n = actions.length;
	wire(mixProIn, 0x90, 0x02, 0x7f);
	assert.equal(actions.length, n + 1);
	assert.deepEqual(actions[n].action, { type: 'deck_sync_toggle', deck: 1 });
	assert.equal(actions[n].deviceId, 'mix-pro-in');

	n = actions.length;
	// Captured encoder direction: low values are up, high values down; the
	// map inverts WebMIDI's generic two's-complement interpretation.
	wire(mixProIn, 0xbf, 0x00, 0x01);
	assert.deepEqual(actions[n].value, { kind: 'relative', delta: -1 });
	wire(mixProIn, 0xbf, 0x00, 0x7e);
	assert.deepEqual(actions[n + 1].value, { kind: 'relative', delta: 1 });
});

test('FLX4 play note -> deck_play_toggle and CFX CC -> filter action', () => {
	const n = actions.length;
	// [PDF] 1-1 PLAY/PAUSE deck 1: Note On ch 1, note 11.
	wire(flx4In, 0x90, 0x0b, 0x7f);
	assert.equal(actions.length, n + 1);
	assert.deepEqual(actions[n].action, { type: 'deck_play_toggle', deck: 1 });
	// [PDF] 3-5 CFX deck 1: CC 23 on the GLOBAL mixer channel 7 (0xb6), hardware-confirmed in #3491.
	wire(flx4In, 0xb6, 0x17, 64);
	assert.equal(actions.length, n + 2);
	assert.deepEqual(actions[n + 1].action, {
		type: 'mixer_channel',
		deck: 1,
		target: 'filter'
	});
});

test('FLX4 unmapped JOG CC -> learn-log unmapped source, no action', () => {
	const n = actions.length;
	// [PDF] 1-4 JOG vinyl on: ch 1 CC 34 - hinted, not bound.
	wire(flx4In, 0xb0, 0x22, 64);
	assert.equal(actions.length, n);
	assert.equal(webmidi.learnLog[0].mapped, false);
	assert.equal(webmidi.learnLog[0].note, 'unmapped source');
});

test('FLX4 program change -> learn-log out of P0 scope, no action', () => {
	const n = actions.length;
	wire(flx4In, 0xc0, 0x05, 0x00);
	assert.equal(actions.length, n);
	assert.equal(webmidi.learnLog[0].mapped, false);
	assert.match(webmidi.learnLog[0].note, /out of P0 scope/);
});

test('DDJ-400 cue-surface bindings dispatch on the DDJ-400 map only', () => {
	const n = actions.length;
	wire(ddjIn, 0x90, 0x54, 0x7f);
	assert.equal(actions.length, n + 1);
	assert.deepEqual(actions[n].action, { type: 'channel_cue', deck: 1 });
	wire(ddjIn, 0xb6, 0x0c, 64);
	assert.equal(actions.length, n + 2);
	assert.equal(actions[n + 1].action.type, 'headphone_mix');
	assert.ok(Math.abs(actions[n + 1].value.value01 - 64 / 127) < 1e-9);
	wire(ddjIn, 0x96, 0x63, 0x7f);
	assert.equal(actions.length, n + 3);
	assert.deepEqual(actions[n + 2].action, { type: 'master_cue', mode: 'latch' });
});

test('DDJ-400 CH CUE LED -> outbound Note On bytes', async () => {
	const rule = ddj400.DDJ400_MAP.leds.find(
		(r) => r.trigger.kind === 'channel_cue_enabled' && r.trigger.deck === 1
	);
	assert.notEqual(rule, undefined);
	ddjOut.sent.length = 0;
	webmidi.sendLed('ddj-in', rule.out.ch, rule.out.note, rule.out.velocityOn);
	await sleep(webmidi.LED_THROTTLE_MS * 3);
	assert.deepEqual(ddjOut.sent, [[0x90, 0x54, 0x7f]]);
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

test('Mixtour Pro VU value uses the production coalesced CC output path', async () => {
	const n = mixProOut.sent.length;
	webmidi.sendCc('mix-pro-in', 1, 0x1f, 6);
	await sleep(50);
	assert.deepEqual(mixProOut.sent.slice(n), [[0xb0, 0x1f, 6]]);
});

// ------------------------------------------- installed map feedback (#10058)

test('installed map init triples are sent once, in order, when the port resolves', () => {
	assert.deepEqual(initSentAtConnect, INIT_TRIPLES);
});

test('a plain rescan of an already-resolved port does not resend init', () => {
	instOut.sent.length = 0;
	fakeAccess.onstatechange({});
	assert.deepEqual(instOut.sent, []);
});

test('re-registering an unrelated map does not resend init', () => {
	instOut.sent.length = 0;
	const unrelated = { vendor: 'X', nameMatch: 'NoSuchPort', bindings: [] };
	webmidi.registerDeviceMap(unrelated, 'installed');
	assert.equal(webmidi.unregisterDeviceMap('NoSuchPort', 'installed'), true);
	assert.deepEqual(instOut.sent, []);
});

test('a hot-plug reconnect resends init', () => {
	instOut.sent.length = 0;
	try {
		instIn.state = 'disconnected';
		fakeAccess.onstatechange({});
		assert.equal(
			webmidi.midiState.devices.some((d) => d.id === 'inst-in'),
			false
		);
		assert.deepEqual(instOut.sent, []);
		instIn.state = 'connected';
		fakeAccess.onstatechange({});
		assert.deepEqual(instOut.sent, INIT_TRIPLES);
	} finally {
		instIn.state = 'connected';
	}
});

test('installed deck_playing LED rule lights the pad when deck 1 plays', async () => {
	const syncLeds = () => {
		// Exactly _syncLeds' body for one device (attachMidiGlue would replace
		// this file's own action handler).
		for (const o of glue.midiLedFeedback(webmidi.getDeviceMap('inst-in'), 'inst-in')) {
			webmidi.sendLed('inst-in', o.ch, o.note, o.velocity);
		}
	};
	try {
		assert.equal(webmidi.getDeviceMap('inst-in').leds.length, 1);
		instOut.sent.length = 0;
		audioEngine.deckStates[1].playing = true;
		syncLeds();
		await sleep(webmidi.LED_THROTTLE_MS * 3);
		assert.deepEqual(instOut.sent, [[0x90, 0x30, 0x7f]]);
		instOut.sent.length = 0;
		audioEngine.deckStates[1].playing = false;
		syncLeds();
		await sleep(webmidi.LED_THROTTLE_MS * 3);
		assert.deepEqual(instOut.sent, [[0x90, 0x30, 0x00]]);
	} finally {
		audioEngine.deckStates[1].playing = false;
	}
});

function grid(bpm, startSec, count) {
	const spb = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: startSec + i * spb }));
}

test('FLX4 BEAT SYNC LED: deck 1 synced to deck 2 lights deck 1 only', () => {
	const ds = audioEngine.deckStates;
	const saved = [1, 2].map((d) => ({
		stable_id: ds[d].stable_id,
		duration_ms: ds[d].duration_ms,
		anlz: ds[d].anlz,
		beat_sync_enabled: ds[d].beat_sync_enabled,
		playing: ds[d].playing
	}));
	try {
		const beats = grid(128, 0.1, 32);
		for (const deck of [1, 2]) {
			Object.assign(ds[deck], {
				stable_id: `track-${deck}`,
				duration_ms: beats.at(-1).t * 1000,
				anlz: { beatgrid: { source: 'rekordbox', beats, beat_count: beats.length, status: 'ok' } }
			});
		}
		ds[1].beat_sync_enabled = true;
		ds[2].beat_sync_enabled = false;
		ds[2].playing = true;
		const out = glue.midiLedFeedback(flx4.FLX4_MAP);
		assert.ok(out.some((o) => o.ch === 1 && o.note === 0x58 && o.velocity === 0x7f));
		assert.ok(out.some((o) => o.ch === 2 && o.note === 0x58 && o.velocity === 0x00));
		ds[1].anlz = null;
		const outGridless = glue.midiLedFeedback(flx4.FLX4_MAP);
		assert.ok(outGridless.some((o) => o.ch === 1 && o.note === 0x58 && o.velocity === 0x00));
	} finally {
		for (const deck of [1, 2]) {
			Object.assign(ds[deck], saved[deck - 1]);
		}
	}
});

test('FLX4 pad mode LEDs: the selected mode is lit, the others unlit', () => {
	try {
		padRuntime.resetControllerPadRuntime();
		let out = glue.midiLedFeedback(flx4.FLX4_MAP);
		assert.ok(out.some((o) => o.ch === 1 && o.note === 0x1b && o.velocity === 0x7f));
		assert.ok(out.some((o) => o.ch === 1 && o.note === 0x1e && o.velocity === 0x00));
		assert.ok(out.some((o) => o.ch === 1 && o.note === 0x22 && o.velocity === 0x00));
		assert.ok(
			!out.some((o) => o.ch === 1 && o.note === 0x20 && o.velocity !== 0x00),
			'BEAT JUMP has no LED rule'
		);
		padRuntime.selectControllerPadMode(
			undefined,
			1,
			'sampler',
			() => {},
			() => {}
		);
		out = glue.midiLedFeedback(flx4.FLX4_MAP);
		assert.ok(out.some((o) => o.ch === 1 && o.note === 0x22 && o.velocity === 0x7f));
		assert.ok(out.some((o) => o.ch === 1 && o.note === 0x1b && o.velocity === 0x00));
		assert.ok(out.some((o) => o.ch === 1 && o.note === 0x1e && o.velocity === 0x00));
		assert.ok(out.some((o) => o.ch === 2 && o.note === 0x1b && o.velocity === 0x7f));
	} finally {
		padRuntime.resetControllerPadRuntime();
	}
});

// ------------------------------------------- per-device shift layer (CTRL)

// Test-only map: SHIFT on note 0x3f, a pad on note 0x10 whose shifted twin
// fires a different cue slot. Two same-named boxes resolve to it.
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

let shiftMapRegistered = false;

function _connectShiftBoxes() {
	for (const input of [shiftAIn, shiftBIn]) {
		input.state = 'connected';
		fakeAccess.inputs.set(input.id, input);
	}
	// registerDeviceMap re-resolves and rescans; later calls rescan directly.
	if (!shiftMapRegistered) webmidi.registerDeviceMap(SHIFT_TEST_MAP);
	shiftMapRegistered = true;
	fakeAccess.onstatechange();
}

test('shift on device A never shifts device B; A itself fires its shifted twin', () => {
	_connectShiftBoxes();
	const rearmed = [];
	webmidi.registerTakeoverRearm((id) => rearmed.push(id));
	wire(shiftAIn, 0x90, 0x3f, 0x7f); // A: SHIFT down
	assert.deepEqual(rearmed, ['shift-a'], 'takeover re-arm hits the pressing device only');
	assert.equal(webmidi.learnLog[0].note, 'shift held');
	assert.equal(webmidi.learnLog[0].deviceId, 'shift-a');
	assert.deepEqual(webmidi.midiState.shiftHeldByDevice, { 'shift-a': true });
	assert.equal(webmidi.midiState.shiftHeld, true, 'aggregate flag stays for the UI');

	const n = actions.length;
	wire(shiftBIn, 0x90, 0x10, 0x7f);
	assert.deepEqual(actions[n].action, { type: 'deck_play_toggle', deck: 1 });
	assert.equal(actions[n].deviceId, 'shift-b');
	wire(shiftAIn, 0x90, 0x10, 0x7f);
	assert.deepEqual(actions[n + 1].action, { type: 'deck_cue', deck: 1 });
	assert.equal(actions[n + 1].deviceId, 'shift-a');

	wire(shiftAIn, 0x80, 0x3f, 0x00); // A: SHIFT up
	assert.deepEqual(webmidi.midiState.shiftHeldByDevice, {});
	assert.equal(webmidi.midiState.shiftHeld, false);
});

test('unplugging a device with shift held clears its entry (no stuck layer)', () => {
	_connectShiftBoxes();
	wire(shiftAIn, 0x90, 0x3f, 0x7f);
	shiftAIn.state = 'disconnected';
	fakeAccess.inputs.delete(shiftAIn.id);
	fakeAccess.onstatechange();
	assert.equal(webmidi.midiState.shiftHeldByDevice['shift-a'], undefined);
	assert.equal(webmidi.midiState.shiftHeld, false);
	_connectShiftBoxes();
	const n = actions.length;
	wire(shiftAIn, 0x90, 0x10, 0x7f);
	assert.deepEqual(actions[n].action, { type: 'deck_play_toggle', deck: 1 });
});

// Keep last: it tears the runtime down.
test('releaseMidiInputs clears every device shift entry', () => {
	_connectShiftBoxes();
	wire(shiftAIn, 0x90, 0x3f, 0x7f);
	wire(shiftBIn, 0x90, 0x3f, 0x7f);
	assert.equal(Object.keys(webmidi.midiState.shiftHeldByDevice).length, 2);
	webmidi.releaseMidiInputs();
	assert.deepEqual(webmidi.midiState.shiftHeldByDevice, {});
	assert.equal(webmidi.midiState.shiftHeld, false);
});
