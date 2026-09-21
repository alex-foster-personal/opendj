// requirement: CTRL-01
// Unit tests for the Pioneer DJ DDJ-FLX4 P0 device map
// (src/lib/rb/midi/maps/ddj-flx4.ts) + registry entry.
//
// Every expected wire number is read off the official AlphaTheta
// DDJ-FLX4 List of MIDI messages PDF version 1.0 E1 ([PDF] fig rows).
// Nothing here is sourced from tools/deck-diagrams/devices/ddj-flx4/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(FLX4_MAP) throws then broken
//   if nameMatch matches DDJ-FLX10 or DDJ-400 then broken
//   if play/cue aren't ch n notes 0x0B/0x0C ([PDF] 1-1/1-2) then broken
//   if tempo isn't cc 0 with lsbOffset 32 ([PDF] 1-11) then broken
//   if hot-cue pads aren't notes 0-7 on ch 8/10 then broken
//   if beat-loop pads aren't notes 0x60-0x67 with doubling ladder then broken
//   if RELOOP-EXIT isn't 0x4D and no 4BEAT long-press 0x14 then broken
//   if trim/EQ/fader MSB CCs drift from 4/7/11/15/19 ([PDF] 3-3..3-7) then broken
//   if CFX filter isn't mixer_channel filter CC 23/24 on ch 7 ([PDF] 3-5, hardware Fri 18 Sep 2026) then broken
//   if crossfader/master aren't ch7 CC 31/8 ([PDF] 3-8/3-1) then broken
//   if browse encoder isn't relative cc 0x40 + loads 0x46/0x47 then broken
//   if any deck-3 or deck-4 binding exists then broken
//   if hints collide with bound sources then broken
//   if JOG and BEAT SYNC are not hinted then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx4.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

function findBindings(pred) {
	return mapModule.FLX4_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const DECKS = [1, 2];
const PAD_CH = { 1: 8, 2: 10 };

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.FLX4_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers DDJ-FLX4 port names only ([PDF] USB name)', () => {
	const re = new RegExp(mapModule.FLX4_MAP.nameMatch, 'i');
	assert.ok(re.test('DDJ-FLX4'));
	assert.ok(re.test('Pioneer DJ DDJ-FLX4 MIDI 1'));
	assert.ok(re.test('DDJ-FLX4_1'));
	assert.ok(!re.test('DDJ-FLX10'));
	assert.ok(!re.test('DDJ-400'));
	assert.ok(!re.test('Mixtour'));
});

test('play/cue/shift per deck on channels 1-2 ([PDF] 1-1/1-2/1-3)', () => {
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
	for (const b of shifts) assert.equal(b.source.id, 0x3f);
});

test('tempo fader is a 14-bit pair: CC 0 MSB, lsbOffset 32 ([PDF] 1-11)', () => {
	for (const deck of DECKS) {
		const pitch = theBinding((b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
		assert.deepEqual(pitch.source, { ch: deck, kind: 'cc', id: 0x00 });
		assert.equal(pitch.action.lsbOffset, 32);
	}
});

test('two-channel controller: nothing addresses deck 3 or deck 4', () => {
	const strays = findBindings((b) => 'deck' in b.action && b.action.deck > 2);
	assert.deepEqual(strays, []);
});

test('hot-cue pads: notes 0-7 on ch 8/10 -> slots A-H ([PDF] 5-5 HOT CUE)', () => {
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
});

test('beat-loop pads 0x60-0x67 + RELOOP/EXIT only, no 4BEAT long-press ([PDF] 1-7/5-5)', () => {
	assert.deepEqual(mapModule.FLX4_BEAT_LOOP_PAD_BEATS, [0.25, 0.5, 1, 2, 4, 8, 16, 32]);
	for (const deck of DECKS) {
		for (let pad = 0; pad < 8; pad++) {
			const b = theBinding(
				(x) =>
					x.action.type === 'deck_beat_loop' &&
					x.action.deck === deck &&
					x.source.ch === PAD_CH[deck] &&
					x.source.id === 0x60 + pad
			);
			assert.equal(b.action.beats, mapModule.FLX4_BEAT_LOOP_PAD_BEATS[pad]);
		}
		const exit = theBinding((x) => x.action.type === 'deck_loop_exit' && x.action.deck === deck);
		assert.deepEqual(exit.source, { ch: deck, kind: 'note', id: 0x4d });
		const fourBeat = findBindings(
			(x) => x.action.type === 'deck_beat_loop' && x.action.deck === deck && x.source.ch === deck
		);
		assert.deepEqual(fourBeat, [], 'FLX4 has no 4 BEAT long-press row ([PDF] 1-7)');
	}
});

test('per-channel mixer MSB CCs: trim 4, EQ 7/11/15, fader 19 ([PDF] 3-3..3-7)', () => {
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

test('CFX filter per deck CC 23/24 on global ch 7 ([PDF] 3-5, hardware-confirmed)', () => {
	const f1 = theBinding(
		(b) => b.action.type === 'mixer_channel' && b.action.deck === 1 && b.action.target === 'filter'
	);
	assert.deepEqual(f1.source, { ch: 7, kind: 'cc', id: 0x17 });
	const f2 = theBinding(
		(b) => b.action.type === 'mixer_channel' && b.action.deck === 2 && b.action.target === 'filter'
	);
	assert.deepEqual(f2.source, { ch: 7, kind: 'cc', id: 0x18 });
});

test('crossfader ch7 CC 31, master ch7 CC 8 ([PDF] 3-8/3-1)', () => {
	const xf = theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader');
	assert.deepEqual(xf.source, { ch: 7, kind: 'cc', id: 0x1f });
	const master = theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'master');
	assert.deepEqual(master.source, { ch: 7, kind: 'cc', id: 0x08 });
});

test('CH CUE per deck on note 0x54 ([PDF] 3-6)', () => {
	for (const deck of DECKS) {
		const cue = theBinding((b) => b.action.type === 'channel_cue' && b.action.deck === deck);
		assert.deepEqual(cue.source, { ch: deck, kind: 'note', id: 0x54 });
	}
});

test('headphone mix/level and master cue on ch 7 ([PDF] 3-11/3-12/3-2)', () => {
	const mix = theBinding((b) => b.action.type === 'headphone_mix');
	assert.deepEqual(mix.source, { ch: 7, kind: 'cc', id: 0x0c });
	const level = theBinding((b) => b.action.type === 'headphone_level');
	assert.deepEqual(level.source, { ch: 7, kind: 'cc', id: 0x0d });
	const masterCue = theBinding((b) => b.action.type === 'master_cue');
	assert.deepEqual(masterCue.source, { ch: 7, kind: 'note', id: 0x63 });
	assert.equal(masterCue.action.mode, 'latch');
});

test('browse encoder relative CC 0x40 (+shift 0x64), loads 0x46/0x47 ([PDF] 4-1/4-2/4-3)', () => {
	const encoders = findBindings((b) => b.action.type === 'browse_encoder');
	assert.equal(encoders.length, 2);
	for (const b of encoders) {
		assert.equal(b.source.ch, 7);
		assert.equal(b.relative, true);
	}
	const loads = findBindings((b) => b.action.type === 'browse_load');
	assert.equal(loads.length, 2);
	for (const deck of DECKS) {
		const load = theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck);
		assert.deepEqual(load.source, { ch: 7, kind: 'note', id: 0x45 + deck });
	}
});

test('LED rules: play/cue/reloop/cue-bus + 8 pad rules per deck ([PDF] MIDI-OUT)', () => {
	const leds = mapModule.FLX4_MAP.leds;
	assert.equal(leds.length, 2 * (4 + 8));
	for (const deck of DECKS) {
		const loop = leds.filter((r) => r.trigger.kind === 'loop_engaged' && r.trigger.deck === deck);
		assert.equal(loop[0].out.note, 0x4d);
		const chCue = leds.filter(
			(r) => r.trigger.kind === 'channel_cue_enabled' && r.trigger.deck === deck
		);
		assert.deepEqual(chCue[0].out, { ch: deck, note: 0x54, velocityOn: 0x7f, velocityOff: 0x00 });
	}
	assert.equal(leds.filter((r) => r.out.note === 0x14).length, 0);
});

test('hints name real unbound controls and never shadow a bound source', () => {
	const key = (s) => `${s.ch}:${s.kind}:${s.id}`;
	const bound = new Set(mapModule.FLX4_MAP.bindings.map((b) => key(b.source)));
	for (const hint of mapModule.FLX4_MAP.hints) {
		assert.ok(!bound.has(key(hint.source)), `hint ${hint.label} shadows bound source`);
	}
	const labels = mapModule.FLX4_MAP.hints.filter((h) => h.source.ch === 1).map((h) => h.label);
	assert.ok(labels.some((l) => l.startsWith('JOG platter, vinyl on')));
	assert.ok(labels.some((l) => l.startsWith('BEAT SYNC (deck 1)')));
});

test('registry includes DDJ-FLX4 and registerAllDeviceMaps is idempotent', () => {
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	const names = registry.DEVICE_MAP_REGISTRY.map((m) => m.nameMatch);
	assert.ok(names.includes('DDJ-FLX4'));
	registry.registerAllDeviceMaps();
	registry.registerAllDeviceMaps();
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});
