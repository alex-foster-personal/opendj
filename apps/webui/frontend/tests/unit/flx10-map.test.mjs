// Unit tests for the DDJ-FLX10 P0 device map
// (src/lib/rb/midi/maps/ddj-flx10.ts) + the maps/index.ts registry.
// Same vite+svelte bootstrap as midi-core.test.mjs (rune modules in the
// import graph need the plugin).
//
// Every expected wire number below is read off the official Pioneer/
// AlphaTheta "DDJ-FLX10 List of MIDI message" PDF ([PDF], spike source
// [1]); Fig. rows quoted per assertion.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(FLX10_MAP) throws then broken (dupes/invalid)
//   if nameMatch doesn't match 'DDJ-FLX10' port names then broken
//   if play/cue aren't ch n notes 0x0B/0x0C ([PDF] D1/D2) then broken
//   if tempo isn't cc 0 with lsbOffset 32 ([PDF] D4) then broken
//   if hot-cue pads aren't notes 0-7 on ch 8/10/12/14 -> slots A-H then broken
//   if beat-loop pads aren't notes 0x60-0x67 with the doubling ladder then broken
//   if trim/EQ/fader MSB CCs drift from 4/7/11/15/19 ([PDF] M2-M6) then broken
//   if crossfader/master aren't ch7 CC 31/8 ([PDF] M1/M8) then broken
//   if browse encoder isn't relative cc 0x40 + loads notes 0x46-0x49 then broken
//   if play LED rule isn't ch n note 0x0B 0x7F/0x00 then broken
//   if pad LED rules aren't 8 per deck on pad channels then broken
//   if registerAllDeviceMaps isn't idempotent then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx10.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

function findBindings(pred) {
	return mapModule.FLX10_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const PAD_CH = { 1: 8, 2: 10, 3: 12, 4: 14 }; // [PDF] p.1 channel table

// -------------------------------------------------------------- validation

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.FLX10_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers FLX10 port names and not the Mixtour', () => {
	const re = new RegExp(mapModule.FLX10_MAP.nameMatch, 'i');
	assert.ok(re.test('DDJ-FLX10'));
	assert.ok(re.test('Pioneer DJ DDJ-FLX10 MIDI 1'));
	assert.ok(!re.test('Reloop Mixtour'));
});

// --------------------------------------------------------------- transport

test('play/cue/shift per deck on channels 1-4 ([PDF] D1/D2/D25)', () => {
	for (const deck of [1, 2, 3, 4]) {
		const play = theBinding(
			(b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck
		);
		assert.deepEqual(play.source, { ch: deck, kind: 'note', id: 0x0b });
		const cue = theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.deepEqual(cue.source, { ch: deck, kind: 'note', id: 0x0c });
	}
	const shifts = findBindings((b) => b.action.type === 'shift_modifier');
	assert.equal(shifts.length, 4);
	for (const b of shifts) assert.equal(b.source.id, 0x3f); // [PDF] D25
});

test('tempo fader is a 14-bit pair: CC 0 MSB, lsbOffset 32 ([PDF] D4)', () => {
	for (const deck of [1, 2, 3, 4]) {
		const pitch = theBinding((b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
		assert.deepEqual(pitch.source, { ch: deck, kind: 'cc', id: 0x00 });
		assert.equal(pitch.action.lsbOffset, 32);
		assert.notEqual(pitch.invert, true); // "-" side Min matches glue 0 -> -range
	}
});

// -------------------------------------------------------------------- pads

test('hot-cue pads: notes 0-7 on pad channels -> slots A-H ([PDF] P1-P8)', () => {
	const slots = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
	for (const deck of [1, 2, 3, 4]) {
		for (let pad = 0; pad < 8; pad++) {
			const b = theBinding(
				(x) =>
					x.action.type === 'deck_hot_cue' && x.action.deck === deck && x.action.slot === slots[pad]
			);
			assert.deepEqual(b.source, { ch: PAD_CH[deck], kind: 'note', id: pad });
		}
	}
	assert.equal(findBindings((b) => b.action.type === 'deck_hot_cue').length, 32);
});

test('beat-loop pads: notes 0x60-0x67 with the doubling ladder ([PDF] P1-P8)', () => {
	assert.deepEqual(mapModule.FLX10_BEAT_LOOP_PAD_BEATS, [0.25, 0.5, 1, 2, 4, 8, 16, 32]);
	for (const deck of [1, 2, 3, 4]) {
		for (let pad = 0; pad < 8; pad++) {
			const b = theBinding(
				(x) =>
					x.action.type === 'deck_beat_loop' &&
					x.action.deck === deck &&
					x.source.ch === PAD_CH[deck] &&
					x.source.id === 0x60 + pad
			);
			assert.equal(b.action.beats, mapModule.FLX10_BEAT_LOOP_PAD_BEATS[pad]);
		}
		// 4 BEAT/EXIT button pair on the deck channel ([PDF] D16).
		const fourBeat = theBinding(
			(x) =>
				x.action.type === 'deck_beat_loop' && x.action.deck === deck && x.source.ch === deck
		);
		assert.deepEqual(fourBeat.source, { ch: deck, kind: 'note', id: 0x14 });
		assert.equal(fourBeat.action.beats, 4);
		const exit = theBinding((x) => x.action.type === 'deck_loop_exit' && x.action.deck === deck);
		assert.deepEqual(exit.source, { ch: deck, kind: 'note', id: 0x50 });
	}
});

// ------------------------------------------------------------------- mixer

test('per-channel mixer MSB CCs: trim 4, EQ 7/11/15, fader 19 ([PDF] M2-M6)', () => {
	for (const deck of [1, 2, 3, 4]) {
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

test('browse encoder relative CC 0x40 (+shift 0x64), loads 0x46-0x49 ([PDF] B1)', () => {
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
	for (const deck of [1, 2, 3, 4]) {
		const load = theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck);
		assert.deepEqual(load.source, { ch: 7, kind: 'note', id: 0x45 + deck });
	}
});

// -------------------------------------------------------------------- LEDs

test('LED rules: play/cue/loop buttons + 8 pad rules per deck', () => {
	const leds = mapModule.FLX10_MAP.leds;
	assert.equal(leds.length, 4 * (3 + 8));
	for (const deck of [1, 2, 3, 4]) {
		const play = leds.filter((r) => r.trigger.kind === 'deck_playing' && r.trigger.deck === deck);
		assert.equal(play.length, 1);
		assert.deepEqual(play[0].out, { ch: deck, note: 0x0b, velocityOn: 0x7f, velocityOff: 0x00 });
		const loaded = leds.filter((r) => r.trigger.kind === 'deck_loaded' && r.trigger.deck === deck);
		assert.equal(loaded[0].out.note, 0x0c);
		const loop = leds.filter((r) => r.trigger.kind === 'loop_engaged' && r.trigger.deck === deck);
		assert.equal(loop[0].out.note, 0x14);
		const pads = leds.filter(
			(r) => r.trigger.kind === 'hot_cue_present' && r.trigger.deck === deck
		);
		assert.equal(pads.length, 8);
		for (const r of pads) {
			assert.equal(r.out.ch, PAD_CH[deck]);
			assert.ok(r.out.note >= 0 && r.out.note <= 7);
			assert.ok(r.out.velocityOn >= 1 && r.out.velocityOn <= 127); // colour number range
			assert.equal(r.out.velocityOff, 0x00);
		}
	}
});

test('flx10PadColorVelocity stays inside the 1-127 colour range', () => {
	for (const idx of [null, 0, 3, 15]) {
		const v = mapModule.flx10PadColorVelocity(idx);
		assert.ok(v >= 1 && v <= 127);
	}
});

// ---------------------------------------------------------------- registry

test('registry holds every map and registerAllDeviceMaps is idempotent', () => {
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	const names = registry.DEVICE_MAP_REGISTRY.map((m) => m.nameMatch);
	assert.deepEqual(names, [
		'DDJ-FLX10',
		'DDJ-400',
		'\\bMixtour\\s+Pro\\b',
		'\\bMixtour\\b(?:$|\\S|\\s+(?:$|[^\\sP]|P(?:$|[^r])|Pr(?:$|[^o])|Pro\\w))',
		'DDJ-FLX4'
	]);
	registry.registerAllDeviceMaps();
	registry.registerAllDeviceMaps(); // second call must no-op, not throw
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});
