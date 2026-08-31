// Unit tests for the Pioneer DJ DDJ-400 P0 device map
// (src/lib/rb/midi/maps/ddj-400.ts) + its entry in the maps/index.ts registry.
// Same vite+svelte bootstrap as flx10-map.test.mjs (rune modules in the
// import graph need the plugin).
//
// Every expected wire number below is read off the official Pioneer DJ
// "DDJ-400 List of MIDI messages" PDF version 1.00 ([PDF], archived at
// docs/controller/reference/DDJ-400_MIDI_Message_List_E1.pdf); Fig. rows
// quoted per assertion. Nothing here is sourced from the bootstrap stub
// tools/deck-diagrams/devices/ddj-400/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(DDJ400_MAP) throws then broken (dupes/invalid)
//   if nameMatch matches 'DDJ-FLX10' or 'DDJ-4000' then broken (over-broad)
//   if play/cue aren't ch n notes 0x0B/0x0C ([PDF] D1/D2) then broken
//   if tempo isn't cc 0 with lsbOffset 32 ([PDF] D4) then broken
//   if hot-cue pads aren't notes 0-7 on ch 8/10 -> slots A-H then broken
//   if beat-loop pads aren't notes 0x60-0x67 with the doubling ladder then broken
//   if 4BEAT long-press isn't note 0x14 / RELOOP-EXIT isn't 0x4D then broken
//   if trim/EQ/fader MSB CCs drift from 4/7/11/15/19 ([PDF] M2-M6) then broken
//   if crossfader/master aren't ch7 CC 31/8 ([PDF] M1/M8) then broken
//   if browse encoder isn't relative cc 0x40 + loads notes 0x46/0x47 then broken
//   if any deck-3 or deck-4 binding exists then broken (2-channel controller)
//   if a LED rule targets note 0x14 (no MIDI-OUT column) then broken
//   if hints collide with bound sources then broken (hint would never show)

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
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-400.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

function findBindings(pred) {
	return mapModule.DDJ400_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const DECKS = [1, 2];
const PAD_CH = { 1: 8, 2: 10 }; // [PDF] p.1 channel assignment table

// -------------------------------------------------------------- validation

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.DDJ400_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers DDJ-400 port names only', () => {
	const re = new RegExp(mapModule.DDJ400_MAP.nameMatch, 'i');
	assert.ok(re.test('DDJ-400'));
	assert.ok(re.test('Pioneer DJ DDJ-400 MIDI 1'));
	assert.ok(!re.test('DDJ-FLX10'));
	assert.ok(!re.test('Reloop Mixtour'));
});

// --------------------------------------------------------------- transport

test('play/cue/shift per deck on channels 1-2 ([PDF] D1/D2/D11)', () => {
	for (const deck of DECKS) {
		const play = theBinding(
			(b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck
		);
		assert.deepEqual(play.source, { ch: deck, kind: 'note', id: 0x0b });
		const cue = theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.deepEqual(cue.source, { ch: deck, kind: 'note', id: 0x0c });
	}
	const shifts = findBindings((b) => b.action.type === 'shift_modifier');
	assert.equal(shifts.length, 2);
	for (const b of shifts) assert.equal(b.source.id, 0x3f); // [PDF] D11
});

test('tempo fader is a 14-bit pair: CC 0 MSB, lsbOffset 32 ([PDF] D4)', () => {
	for (const deck of DECKS) {
		const pitch = theBinding((b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
		assert.deepEqual(pitch.source, { ch: deck, kind: 'cc', id: 0x00 });
		assert.equal(pitch.action.lsbOffset, 32);
		assert.notEqual(pitch.invert, true); // "-" side Min matches glue 0 -> -range
	}
});

test('two-channel controller: nothing addresses deck 3 or deck 4', () => {
	const strays = findBindings((b) => 'deck' in b.action && b.action.deck > 2);
	assert.deepEqual(strays, []);
});

// -------------------------------------------------------------------- pads

test('hot-cue pads: notes 0-7 on ch 8/10 -> slots A-H ([PDF] p.2-3 P1-P8)', () => {
	const slots = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
	for (const deck of DECKS) {
		for (let pad = 0; pad < 8; pad++) {
			const b = theBinding(
				(x) =>
					x.action.type === 'deck_hot_cue' && x.action.deck === deck && x.action.slot === slots[pad]
			);
			assert.deepEqual(b.source, { ch: PAD_CH[deck], kind: 'note', id: pad });
		}
	}
	assert.equal(findBindings((b) => b.action.type === 'deck_hot_cue').length, 16);
});

test('beat-loop pads 0x60-0x67 + 4BEAT long-press + RELOOP/EXIT ([PDF] D6/D8)', () => {
	assert.deepEqual(mapModule.DDJ400_BEAT_LOOP_PAD_BEATS, [0.25, 0.5, 1, 2, 4, 8, 16, 32]);
	for (const deck of DECKS) {
		for (let pad = 0; pad < 8; pad++) {
			const b = theBinding(
				(x) =>
					x.action.type === 'deck_beat_loop' &&
					x.action.deck === deck &&
					x.source.ch === PAD_CH[deck] &&
					x.source.id === 0x60 + pad
			);
			assert.equal(b.action.beats, mapModule.DDJ400_BEAT_LOOP_PAD_BEATS[pad]);
		}
		// [PDF] p.1 D6 LOOP IN/4BEAT LONG press: note 20 (0x14) on the deck channel.
		const fourBeat = theBinding(
			(x) => x.action.type === 'deck_beat_loop' && x.action.deck === deck && x.source.ch === deck
		);
		assert.deepEqual(fourBeat.source, { ch: deck, kind: 'note', id: 0x14 });
		assert.equal(fourBeat.action.beats, 4);
		// [PDF] p.1 D8 RELOOP/EXIT: note 77 (0x4D) - NOT the FLX10's 0x50.
		const exit = theBinding((x) => x.action.type === 'deck_loop_exit' && x.action.deck === deck);
		assert.deepEqual(exit.source, { ch: deck, kind: 'note', id: 0x4d });
	}
});

// ------------------------------------------------------------------- mixer

test('per-channel mixer MSB CCs: trim 4, EQ 7/11/15, fader 19 ([PDF] M2-M6)', () => {
	for (const deck of DECKS) {
		const cases = [
			[(a) => a.target === 'trim', 0x04],
			[(a) => a.target === 'eq' && a.band === 'high', 0x07],
			[(a) => a.target === 'eq' && a.band === 'mid', 0x0b],
			[(a) => a.target === 'eq' && a.band === 'low', 0x0f],
			[(a) => a.target === 'fader', 0x13]
		];
		for (const [match, cc] of cases) {
			const b = theBinding(
				(x) => x.action.type === 'mixer_channel' && x.action.deck === deck && match(x.action)
			);
			assert.deepEqual(b.source, { ch: deck, kind: 'cc', id: cc });
		}
	}
});

test('crossfader ch7 CC 31, master ch7 CC 8 ([PDF] M1/M8)', () => {
	const xf = theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader');
	assert.deepEqual(xf.source, { ch: 7, kind: 'cc', id: 0x1f });
	assert.notEqual(xf.invert, true); // Min at left = engine full A at 0
	const master = theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'master');
	assert.deepEqual(master.source, { ch: 7, kind: 'cc', id: 0x08 });
});

// ------------------------------------------------------------------ browse

test('browse encoder relative CC 0x40 (+shift 0x64), loads 0x46/0x47 ([PDF] B1/B2)', () => {
	const encoders = findBindings((b) => b.action.type === 'browse_encoder');
	assert.equal(encoders.length, 2);
	assert.deepEqual(
		encoders.map((b) => b.source.id).sort((a, b) => a - b),
		[0x40, 0x64]
	);
	for (const b of encoders) {
		assert.equal(b.source.ch, 7);
		assert.equal(b.relative, true);
	}
	const loads = findBindings((b) => b.action.type === 'browse_load');
	assert.equal(loads.length, 2); // no deck 3/4 LOAD buttons on this unit
	for (const deck of DECKS) {
		const load = theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck);
		assert.deepEqual(load.source, { ch: 7, kind: 'note', id: 0x45 + deck });
	}
});

// -------------------------------------------------------------------- LEDs

test('LED rules: play/cue/reloop + 8 pad rules per deck, on/off only', () => {
	const leds = mapModule.DDJ400_MAP.leds;
	assert.equal(leds.length, 2 * (3 + 8));
	for (const deck of DECKS) {
		const play = leds.filter((r) => r.trigger.kind === 'deck_playing' && r.trigger.deck === deck);
		assert.equal(play.length, 1);
		assert.deepEqual(play[0].out, { ch: deck, note: 0x0b, velocityOn: 0x7f, velocityOff: 0x00 });
		const loaded = leds.filter((r) => r.trigger.kind === 'deck_loaded' && r.trigger.deck === deck);
		assert.equal(loaded[0].out.note, 0x0c);
		// Loop feedback rides RELOOP/EXIT (0x4D): the 4BEAT row (0x14) prints
		// no MIDI-OUT column in the [PDF], so lighting it would be invented.
		const loop = leds.filter((r) => r.trigger.kind === 'loop_engaged' && r.trigger.deck === deck);
		assert.equal(loop[0].out.note, 0x4d);
		const pads = leds.filter((r) => r.trigger.kind === 'hot_cue_present' && r.trigger.deck === deck);
		assert.equal(pads.length, 8);
		for (const r of pads) {
			assert.equal(r.out.ch, PAD_CH[deck]);
			assert.ok(r.out.note >= 0 && r.out.note <= 7);
			assert.equal(r.out.velocityOn, 0x7f); // single-colour pads, no palette
			assert.equal(r.out.velocityOff, 0x00);
		}
	}
	assert.equal(
		leds.filter((r) => r.out.note === 0x14).length,
		0,
		'note 0x14 has no MIDI-OUT column in the PDF'
	);
});

// ------------------------------------------------------------------- hints

test('hints name real unbound controls and never shadow a bound source', () => {
	const key = (s) => `${s.ch}:${s.kind}:${s.id}`;
	const bound = new Set(mapModule.DDJ400_MAP.bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const hint of mapModule.DDJ400_MAP.hints) {
		const k = key(hint.source);
		assert.ok(!bound.has(k), `hint ${hint.label} shadows a bound source ${k}`);
		assert.ok(!seen.has(k), `duplicate hint source ${k}`);
		seen.add(k);
		assert.ok(hint.label.length > 0);
	}
	// Spot-check two cited rows: jog platter vinyl-on CC 34 ([PDF] D3) and
	// BEAT SYNC note 88 ([PDF] D5), both deck 1.
	const labels = mapModule.DDJ400_MAP.hints.filter((h) => h.source.ch === 1).map((h) => h.label);
	assert.ok(labels.some((l) => l.startsWith('JOG platter, vinyl on')));
	assert.ok(labels.some((l) => l.startsWith('BEAT SYNC (deck 1)')));
});

// ---------------------------------------------------------------- registry

test('registry includes the DDJ-400 map and stays idempotent', () => {
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	const names = registry.DEVICE_MAP_REGISTRY.map((m) => m.nameMatch);
	assert.ok(names.includes('DDJ-400'));
	registry.registerAllDeviceMaps();
	registry.registerAllDeviceMaps(); // second call must no-op, not throw
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});
