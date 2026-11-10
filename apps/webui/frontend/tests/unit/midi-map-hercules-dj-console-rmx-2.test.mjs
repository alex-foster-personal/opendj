// requirement: MIDI-12019
// Unit tests for the Hercules DJ Console RMX 2 live device map
// (src/lib/rb/midi/maps/hercules-dj-console-rmx-2.ts) + its entry in the maps registry.
//
// Every expected wire number is read off Hercules RMX2_MIDI_Commands.pdf,
// Appendix – DJConsole RMX2 MIDI Controls, [PDF] I Midi In / II Midi Out /
// LEDs control on Rmx2. Nothing here is sourced from
// tools/deck-diagrams/devices/hercules-dj-console-rmx-2/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(HERCULES_DJ_CONSOLE_RMX_2_MAP) throws then broken
//   if nameMatch misses DJConsole RMX2 / Hercules DJ Console RMX 2 then broken
//   if nameMatch matches original RMX or RMX Advanced then broken
//   if play/cue/sync/load aren't ch 1 notes 0x21/0x22/0x23/0x24 (A) and 0x32/0x33/0x34/0x35 (B) then broken
//   if both channel faders aren't cc 0x3A / 0x4A or the crossfader isn't cc 0x48 then broken
//   if jogs aren't cc 0x30/0x32 (A) and 0x31/0x33 (B) with relative+relativeCentered then broken
//   if cue pads aren't deck_hot_cue A-D on notes 0x09-0x0C / 0x19-0x1C then broken
//   if loop pads aren't deck_beat_loop RMX2_LOOP_PAD_BEATS on 0x0D-0x10 / 0x1D-0x20 then broken
//   if FX pads aren't controller_pad 1-4 on notes 0x01-0x04 / 0x11-0x14 then broken
//   if pitch isn't cc 0x36 / 0x38 with lsbOffset 1 then broken
//   if any binding source repeats, or a hint collides with a binding then broken
//   if an LED rule drives a source no binding owns then broken
//   if the registry does not carry HERCULES_DJ_CONSOLE_RMX_2_MAP then broken
//   if any deck-3 or deck-4 binding exists then broken
//   if init is not [0xb0, 0x7f, 0x7f] then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi;
let mapModule;
let registry;

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-console-rmx-2.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return mapModule.HERCULES_DJ_CONSOLE_RMX_2_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const DECKS = [1, 2];
const PLAY = { 1: 0x21, 2: 0x32 };
const CUE = { 1: 0x22, 2: 0x33 };
const SYNC = { 1: 0x23, 2: 0x34 };
const LOAD = { 1: 0x24, 2: 0x35 };
const JOG = { 1: 0x30, 2: 0x31 };
const JOG_SCRATCH = { 1: 0x32, 2: 0x33 };
const PITCH = { 1: 0x36, 2: 0x38 };
const FADER = { 1: 0x3a, 2: 0x4a };
const FX_PAD0 = { 1: 0x01, 2: 0x11 };
const CUE_PAD0 = { 1: 0x09, 2: 0x19 };
const LOOP_PAD0 = { 1: 0x0d, 2: 0x1d };

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.HERCULES_DJ_CONSOLE_RMX_2_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers RMX 2 port names and rejects original RMX / RMX Advanced', () => {
	const re = new RegExp(mapModule.HERCULES_DJ_CONSOLE_RMX_2_MAP.nameMatch, 'i');
	assert.ok(re.test('DJConsole RMX2'));
	assert.ok(re.test('DJConsole Rmx2'));
	assert.ok(re.test('Hercules DJ Console RMX 2'));
	assert.ok(re.test('Hercules DJ Console RMX2 MIDI'));
	assert.ok(!re.test('Hercules DJ Console RMX'));
	assert.ok(!re.test('DJConsole RMX'));
	assert.ok(!re.test('Hercules DJ Console RMX Advanced'));
	assert.ok(!re.test('DDJ-FLX4'));
	assert.ok(!re.test('Mixtour'));
});

test('transport, load, faders, crossfader and jogs sit on the PDF ids ([PDF] I Midi In)', () => {
	for (const deck of DECKS) {
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck).source,
			{
				ch: 1,
				kind: 'note',
				id: PLAY[deck]
			}
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck).source,
			{
				ch: 1,
				kind: 'note',
				id: CUE[deck]
			}
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck).source,
			{
				ch: 1,
				kind: 'note',
				id: SYNC[deck]
			}
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck).source,
			{
				ch: 1,
				kind: 'note',
				id: LOAD[deck]
			}
		);
		const fader = theBinding(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
		);
		assert.deepEqual(fader.source, { ch: 1, kind: 'cc', id: FADER[deck] });
		const jogs = findBindings((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.equal(jogs.length, 2);
		const byId = Object.fromEntries(jogs.map((b) => [b.source.id, b]));
		assert.deepEqual(byId[JOG[deck]].source, { ch: 1, kind: 'cc', id: JOG[deck] });
		assert.deepEqual(byId[JOG_SCRATCH[deck]].source, { ch: 1, kind: 'cc', id: JOG_SCRATCH[deck] });
		for (const jog of jogs) {
			assert.equal(jog.relative, true);
			assert.equal(jog.relativeCentered, true);
		}
	}
	assert.deepEqual(
		theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader').source,
		{ ch: 1, kind: 'cc', id: 0x48 }
	);
});

test('pads: FX controller_pad 1-4, cue hot cues A-D, loop beat ladder ([PDF] pad banks)', () => {
	const slots = ['A', 'B', 'C', 'D'];
	assert.deepEqual(mapModule.RMX2_LOOP_PAD_BEATS, [1, 2, 4, 8]);
	for (const deck of DECKS) {
		for (let i = 0; i < 4; i++) {
			const fx = theBinding(
				(b) =>
					b.action.type === 'controller_pad' &&
					b.action.deck === deck &&
					b.action.pad === i + 1 &&
					b.action.shifted === false
			);
			assert.deepEqual(fx.source, { ch: 1, kind: 'note', id: FX_PAD0[deck] + i });
			const hot = theBinding(
				(b) =>
					b.action.type === 'deck_hot_cue' && b.action.deck === deck && b.action.slot === slots[i]
			);
			assert.deepEqual(hot.source, { ch: 1, kind: 'note', id: CUE_PAD0[deck] + i });
			const loop = theBinding(
				(b) =>
					b.action.type === 'deck_beat_loop' &&
					b.action.deck === deck &&
					b.source.id === LOOP_PAD0[deck] + i
			);
			assert.equal(loop.action.beats, mapModule.RMX2_LOOP_PAD_BEATS[i]);
			assert.deepEqual(loop.source, { ch: 1, kind: 'note', id: LOOP_PAD0[deck] + i });
		}
	}
});

test('pitch uses lsbOffset 1 on cc 0x36 / 0x38 ([PDF] Pitch fader MSB/LSB)', () => {
	for (const deck of DECKS) {
		const pitch = theBinding((b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
		assert.deepEqual(pitch.source, { ch: 1, kind: 'cc', id: PITCH[deck] });
		assert.equal(pitch.action.lsbOffset, 1);
	}
});

test('two-channel map: nothing binds deck 3 or deck 4; init dumps analog positions', () => {
	assert.deepEqual(
		findBindings((b) => 'deck' in b.action && b.action.deck > 2),
		[]
	);
	assert.deepEqual(mapModule.HERCULES_DJ_CONSOLE_RMX_2_MAP.init, [[0xb0, 0x7f, 0x7f]]);
});

test('every binding source is unique; hints never collide with bindings', () => {
	const bound = new Set();
	for (const b of mapModule.HERCULES_DJ_CONSOLE_RMX_2_MAP.bindings) {
		const k = key(b.source);
		assert.ok(!bound.has(k), `duplicate binding on ${k}`);
		bound.add(k);
	}
	const seen = new Set();
	for (const h of mapModule.HERCULES_DJ_CONSOLE_RMX_2_MAP.hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint ${h.label} collides with a binding on ${k}`);
		assert.ok(!seen.has(k), `hint ${h.label} repeats ${k}`);
		seen.add(k);
		assert.ok(h.label.length > 0);
	}
	for (const k of [
		'1:note:5',
		'1:note:47',
		'1:note:64',
		'1:note:38',
		'1:note:39',
		'1:cc:1',
		'1:cc:52',
		'1:cc:59',
		'1:cc:69',
		'1:note:40'
	]) {
		assert.ok(seen.has(k), `expected a hint on ${k}`);
	}
});

test('LED rules drive only sources the map binds ([PDF] LEDs control on Rmx2)', () => {
	const boundNotes = new Set(
		mapModule.HERCULES_DJ_CONSOLE_RMX_2_MAP.bindings
			.filter((b) => b.source.kind === 'note')
			.map((b) => key(b.source))
	);
	const leds = mapModule.HERCULES_DJ_CONSOLE_RMX_2_MAP.leds;
	assert.ok(
		leds.length >= 2 * (3 + 4 + 4),
		`expected play/cue/sync + 4 hot cues + 4 loops per deck, got ${leds.length}`
	);
	for (const rule of leds) {
		const k = `${rule.out.ch}:note:${rule.out.note}`;
		assert.ok(
			boundNotes.has(k),
			`LED rule ${JSON.stringify(rule.trigger)} on ${k} is not a bound control`
		);
		assert.equal(rule.out.velocityOn, mapModule.RMX2_LED_ON);
		assert.equal(rule.out.velocityOff, mapModule.RMX2_LED_OFF);
	}
	const kinds = new Set(leds.map((r) => r.trigger.kind));
	assert.ok(
		kinds.has('deck_playing') &&
			kinds.has('deck_loaded') &&
			kinds.has('beat_sync_enabled') &&
			kinds.has('hot_cue_present') &&
			kinds.has('loop_beats_engaged')
	);
});

test('the registry carries HERCULES_DJ_CONSOLE_RMX_2_MAP', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(mapModule.HERCULES_DJ_CONSOLE_RMX_2_MAP));
	assert.equal(
		registry.DEVICE_MAP_REGISTRY.filter(
			(m) => m.nameMatch === mapModule.HERCULES_DJ_CONSOLE_RMX_2_MAP.nameMatch
		).length,
		1
	);
});
