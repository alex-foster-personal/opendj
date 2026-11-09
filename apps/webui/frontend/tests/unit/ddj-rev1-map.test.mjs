// requirement: SKIN-10384
// Unit tests for the Pioneer DJ DDJ-REV1 P0 device map
// (src/lib/rb/midi/maps/ddj-rev1.ts) + its entry in the maps/index.ts registry.
//
// Every expected wire number is read off the official "DDJ-REV1 List of MIDI
// message" PDF (E1), [PDF] Fig. rows quoted per assertion. Nothing here is
// sourced from tools/deck-diagrams/devices/ddj-rev1/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(DDJ_REV1_MAP) throws then broken
//   if nameMatch matches DDJ-REV5, DDJ-REV7 or DDJ-FLX4 then broken
//   if SHIFT is not the single channel-7 note 0x3F ([PDF] M11) then broken
//   if play/cue aren't ch n notes 0x0B/0x0C ([PDF] D1/D2) then broken
//   if tempo isn't cc 0 with lsbOffset 32 ([PDF] D4) then broken
//   if AUTO LOOP 0x14 is not a 4-beat loop and its +SHIFT 0x50 not the loop exit ([PDF] D6) then broken
//   if 1/2X and 2X ([PDF] D7/D8) do not scale the loop by 0.5 and 2 then broken
//   if SYNC ([PDF] D9) is not deck_sync_toggle then broken
//   if hot-cue pads aren't notes 0-7 and auto-loop pads 0x10-0x17 on ch 8/10 then broken
//   if FILTER isn't mixer_channel filter CC 23/24 on ch 7 ([PDF] F1) then broken
//   if a headphone_mix binding exists (the REV1 has no MIX knob) then broken
//   if any deck-3 or deck-4 binding exists then broken
//   if hints collide with bound sources or repeat then broken
//   if an LED rule drives a source no binding owns then broken
//   if the registry does not carry DDJ_REV1_MAP then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-rev1.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return mapModule.DDJ_REV1_MAP.bindings.filter(pred);
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
	webmidi.registerDeviceMap(mapModule.DDJ_REV1_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers DDJ-REV1 port names only', () => {
	const re = new RegExp(mapModule.DDJ_REV1_MAP.nameMatch, 'i');
	assert.ok(re.test('DDJ-REV1'));
	assert.ok(re.test('Pioneer DJ DDJ-REV1 MIDI 1'));
	assert.ok(!re.test('DDJ-REV5'));
	assert.ok(!re.test('DDJ-REV7'));
	assert.ok(!re.test('DDJ-FLX4'));
	assert.ok(!re.test('DDJ-400'));
});

test('one SHIFT on channel 7 ([PDF] M11); no deck-channel shift row', () => {
	const shifts = findBindings((b) => b.action.type === 'shift_modifier');
	assert.equal(shifts.length, 1);
	assert.deepEqual(shifts[0].source, { ch: 7, kind: 'note', id: 0x3f });
});

test('transport, tempo, loop and sync per deck ([PDF] D1/D2/D4/D6-D9)', () => {
	for (const deck of DECKS) {
		const on = (type) => theBinding((b) => b.action.type === type && b.action.deck === deck);
		assert.deepEqual(on('deck_play_toggle').source, { ch: deck, kind: 'note', id: 0x0b });
		assert.deepEqual(on('deck_cue').source, { ch: deck, kind: 'note', id: 0x0c });
		const pitch = on('deck_pitch');
		assert.deepEqual(pitch.source, { ch: deck, kind: 'cc', id: 0x00 });
		assert.equal(pitch.action.lsbOffset, 32);
		const fourBeat = theBinding(
			(b) => b.action.type === 'deck_beat_loop' && b.action.deck === deck && b.source.ch === deck
		);
		assert.deepEqual(fourBeat.source, { ch: deck, kind: 'note', id: 0x14 });
		assert.equal(fourBeat.action.beats, 4);
		assert.deepEqual(on('deck_loop_exit').source, { ch: deck, kind: 'note', id: 0x50 });
		const half = theBinding(
			(b) =>
				b.action.type === 'deck_loop_scale' && b.action.deck === deck && b.action.factor === 0.5
		);
		assert.deepEqual(half.source, { ch: deck, kind: 'note', id: 0x12 });
		const double = theBinding(
			(b) => b.action.type === 'deck_loop_scale' && b.action.deck === deck && b.action.factor === 2
		);
		assert.deepEqual(double.source, { ch: deck, kind: 'note', id: 0x13 });
		assert.deepEqual(on('deck_sync_toggle').source, { ch: deck, kind: 'note', id: 0x58 });
	}
});

test('pads: hot cues notes 0-7, auto-loop ladder 0x10-0x17, on ch 8/10 ([PDF] P1-P8)', () => {
	const slots = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
	assert.deepEqual(mapModule.DDJ_REV1_AUTO_LOOP_PAD_BEATS, [0.25, 0.5, 1, 2, 4, 8, 16, 32]);
	for (const deck of DECKS) {
		for (let pad = 0; pad < 8; pad++) {
			const hot = theBinding(
				(x) =>
					x.action.type === 'deck_hot_cue' && x.action.deck === deck && x.action.slot === slots[pad]
			);
			assert.deepEqual(hot.source, { ch: PAD_CH[deck], kind: 'note', id: pad });
			const loop = theBinding(
				(x) =>
					x.action.type === 'deck_beat_loop' &&
					x.action.deck === deck &&
					x.source.ch === PAD_CH[deck] &&
					x.source.id === 0x10 + pad
			);
			assert.equal(loop.action.beats, mapModule.DDJ_REV1_AUTO_LOOP_PAD_BEATS[pad]);
		}
	}
});

test('per-channel mixer MSB CCs and FILTER on channel 7 ([PDF] M2-M7, F1)', () => {
	for (const deck of DECKS) {
		const cases = [
			[(a) => a.target === 'trim', deck, 0x04],
			[(a) => a.target === 'eq' && a.band === 'high', deck, 0x07],
			[(a) => a.target === 'eq' && a.band === 'mid', deck, 0x0b],
			[(a) => a.target === 'eq' && a.band === 'low', deck, 0x0f],
			[(a) => a.target === 'fader', deck, 0x13],
			[(a) => a.target === 'filter', 7, 0x16 + deck]
		];
		for (const [pred, ch, cc] of cases) {
			const b = theBinding(
				(x) => x.action.type === 'mixer_channel' && x.action.deck === deck && pred(x.action)
			);
			assert.deepEqual(b.source, { ch, kind: 'cc', id: cc });
		}
		const cue = theBinding((x) => x.action.type === 'channel_cue' && x.action.deck === deck);
		assert.deepEqual(cue.source, { ch: deck, kind: 'note', id: 0x54 });
	}
});

test('global mixer and browser ([PDF] M1/M8/M9/M10, B1/B2); no headphone MIX knob', () => {
	assert.deepEqual(
		theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader').source,
		{ ch: 7, kind: 'cc', id: 0x1f }
	);
	assert.deepEqual(
		theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'master').source,
		{ ch: 7, kind: 'cc', id: 0x08 }
	);
	assert.deepEqual(theBinding((b) => b.action.type === 'headphone_level').source, {
		ch: 7,
		kind: 'cc',
		id: 0x0d
	});
	assert.deepEqual(
		findBindings((b) => b.action.type === 'headphone_mix'),
		[]
	);
	assert.deepEqual(theBinding((b) => b.action.type === 'master_cue').source, {
		ch: 7,
		kind: 'note',
		id: 0x63
	});
	const browse = findBindings((b) => b.action.type === 'browse_encoder');
	assert.deepEqual(
		browse.map((b) => [b.source.id, b.relative]),
		[
			[0x40, true],
			[0x64, true]
		]
	);
	for (const deck of DECKS) {
		const load = theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck);
		assert.deepEqual(load.source, { ch: 7, kind: 'note', id: 0x45 + deck });
	}
});

test('two-channel map: nothing binds deck 3 or deck 4, nothing sends on channels 3, 4, 9 or 11', () => {
	assert.deepEqual(
		findBindings((b) => 'deck' in b.action && b.action.deck > 2),
		[]
	);
	assert.deepEqual(
		findBindings((b) => [3, 4, 9, 11].includes(b.source.ch)),
		[]
	);
});

test('hints are unique and never collide with bound sources', () => {
	const bound = new Set(mapModule.DDJ_REV1_MAP.bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of mapModule.DDJ_REV1_MAP.hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint ${h.label} collides with a binding on ${k}`);
		assert.ok(!seen.has(k), `hint ${h.label} repeats ${k}`);
		seen.add(k);
		assert.ok(h.label.length > 0);
	}
	// Controls the replica draws inert must be hinted: jog, DECK SELECT, pad modes, FX.
	for (const k of ['1:cc:34', '3:note:114', '4:note:114', '1:note:27', '5:note:80', '5:cc:2']) {
		assert.ok(seen.has(k), `expected a hint on ${k}`);
	}
});

test('LED rules drive only sources the map binds ([PDF] MIDI-OUT rows of bound controls)', () => {
	const boundNotes = new Set(
		mapModule.DDJ_REV1_MAP.bindings
			.filter((b) => b.source.kind === 'note')
			.map((b) => key(b.source))
	);
	const leds = mapModule.DDJ_REV1_MAP.leds;
	assert.ok(
		leds.length >= 2 * (6 + 16),
		`expected play/cue/loop x2/sync/ch cue + 16 pads per deck, got ${leds.length}`
	);
	for (const rule of leds) {
		const k = `${rule.out.ch}:note:${rule.out.note}`;
		assert.ok(
			boundNotes.has(k),
			`LED rule ${JSON.stringify(rule.trigger)} on ${k} is not a bound control`
		);
	}
	const kinds = new Set(leds.map((r) => r.trigger.kind));
	assert.ok(
		kinds.has('beat_sync_enabled') &&
			kinds.has('loop_beats_engaged') &&
			kinds.has('hot_cue_present')
	);
});

test('the registry carries DDJ_REV1_MAP', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(mapModule.DDJ_REV1_MAP));
	assert.equal(registry.DEVICE_MAP_REGISTRY.filter((m) => m.nameMatch === 'DDJ-REV1').length, 1);
});
