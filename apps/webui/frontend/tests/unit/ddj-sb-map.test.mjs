// requirement: SKIN-10858
// Unit tests for the Pioneer DJ DDJ-SB P0 device map
// (src/lib/rb/midi/maps/ddj-sb.ts) + its entry in the maps/index.ts registry.
//
// Every expected wire number is read off the official "DDJ-SB List of MIDI
// messages" PDF (ver. 1.00), [PDF] Fig. rows quoted per assertion. Nothing
// here is sourced from tools/deck-diagrams/devices/ddj-sb/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(DDJ_SB_MAP) throws then broken
//   if nameMatch matches DDJ-SB2 or DDJ-SB3 then broken
//   if SHIFT is not per-deck channel-n note 0x3F ([PDF] fig 8) then broken
//   if play/cue aren't ch n notes 0x0B/0x0C ([PDF] fig 1/2) then broken
//   if tempo isn't cc 0 with lsbOffset 32 ([PDF] fig 5) then broken
//   if SYNC ([PDF] fig 3) is not deck_sync_toggle then broken
//   if hot-cue pads aren't notes 0-3 and auto-loop pads 0x10-0x13 on ch 8/9 then broken
//   if FILTER isn't mixer_channel filter CC 23/24 on ch 7 ([PDF] fig 14) then broken
//   if HEADPHONES MIX isn't ch 7 CC 5 ([PDF] fig 17) then broken
//   if a master or headphone_level binding exists (those knobs are analog) then broken
//   if any deck-3 or deck-4 binding exists then broken
//   if pads use Pioneer 8/10 channels then broken
//   if hints collide with bound sources or repeat then broken
//   if any binding or hint uses a channel-16 placeholder for an analog control then broken
//   if an LED rule drives a source no binding owns then broken
//   if the registry does not carry DDJ_SB_MAP then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-sb.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return mapModule.DDJ_SB_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const DECKS = [1, 2];
const PAD_CH = { 1: 8, 2: 9 };

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.DDJ_SB_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers DDJ-SB port names and rejects SB2/SB3', () => {
	const re = new RegExp(mapModule.DDJ_SB_MAP.nameMatch, 'i');
	assert.ok(re.test('DDJ-SB'));
	assert.ok(re.test('Pioneer DJ DDJ-SB MIDI 1'));
	assert.ok(re.test('DDJ-SB-S'));
	assert.ok(!re.test('DDJ-SB2'));
	assert.ok(!re.test('DDJ-SB3'));
	assert.ok(!re.test('Pioneer DJ DDJ-SB3 MIDI 1'));
	assert.ok(!re.test('DDJ-400'));
});

test('one SHIFT per deck on note 0x3F ([PDF] fig 8); not a channel-7 row', () => {
	const shifts = findBindings((b) => b.action.type === 'shift_modifier');
	assert.equal(shifts.length, 2);
	assert.deepEqual(
		shifts.map((b) => b.source).sort((a, b) => a.ch - b.ch),
		[
			{ ch: 1, kind: 'note', id: 0x3f },
			{ ch: 2, kind: 'note', id: 0x3f }
		]
	);
});

test('transport, tempo and sync per deck ([PDF] fig 1/2/3/5)', () => {
	for (const deck of DECKS) {
		const on = (type) => theBinding((b) => b.action.type === type && b.action.deck === deck);
		assert.deepEqual(on('deck_play_toggle').source, { ch: deck, kind: 'note', id: 0x0b });
		assert.deepEqual(on('deck_cue').source, { ch: deck, kind: 'note', id: 0x0c });
		const pitch = on('deck_pitch');
		assert.deepEqual(pitch.source, { ch: deck, kind: 'cc', id: 0x00 });
		assert.equal(pitch.action.lsbOffset, 32);
		assert.deepEqual(on('deck_sync_toggle').source, { ch: deck, kind: 'note', id: 0x58 });
	}
});

test('pads: hot cues notes 0-3, auto-loop ladder 0x10-0x13, on ch 8/9 ([PDF] fig 31-34)', () => {
	const slots = ['A', 'B', 'C', 'D'];
	assert.deepEqual(mapModule.DDJ_SB_AUTO_LOOP_PAD_BEATS, [1, 2, 4, 8]);
	for (const deck of DECKS) {
		for (let pad = 0; pad < 4; pad++) {
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
			assert.equal(loop.action.beats, mapModule.DDJ_SB_AUTO_LOOP_PAD_BEATS[pad]);
		}
		assert.equal(
			findBindings(
				(x) => x.action.type === 'deck_hot_cue' && x.action.deck === deck && x.source.id > 3
			).length,
			0
		);
	}
});

test('per-channel mixer MSB CCs and FILTER on channel 7 ([PDF] fig 10-15)', () => {
	for (const deck of DECKS) {
		const cases = [
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
		assert.deepEqual(
			findBindings(
				(x) =>
					x.action.type === 'mixer_channel' && x.action.deck === deck && x.action.target === 'trim'
			),
			[]
		);
	}
});

test('global mixer and browser ([PDF] fig 9/17/27-29); analog master and phones vol unbound', () => {
	assert.deepEqual(
		theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader').source,
		{ ch: 7, kind: 'cc', id: 0x1f }
	);
	assert.deepEqual(theBinding((b) => b.action.type === 'headphone_mix').source, {
		ch: 7,
		kind: 'cc',
		id: 0x05
	});
	assert.deepEqual(
		findBindings((b) => b.action.type === 'mixer_global' && b.action.target === 'master'),
		[]
	);
	assert.deepEqual(
		findBindings((b) => b.action.type === 'headphone_level'),
		[]
	);
	assert.deepEqual(
		findBindings((b) => b.action.type === 'master_cue'),
		[]
	);
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

test('two-channel map: nothing binds deck 3 or 4, nothing sends on channels 3, 4, 10 or 11', () => {
	assert.deepEqual(
		findBindings((b) => 'deck' in b.action && b.action.deck > 2),
		[]
	);
	assert.deepEqual(
		findBindings((b) => [3, 4, 10, 11].includes(b.source.ch)),
		[]
	);
});

test('hints are unique and never collide with bound sources', () => {
	const bound = new Set(mapModule.DDJ_SB_MAP.bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of mapModule.DDJ_SB_MAP.hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint ${h.label} collides with a binding on ${k}`);
		assert.ok(!seen.has(k), `hint ${h.label} repeats ${k}`);
		seen.add(k);
		assert.ok(h.label.length > 0);
	}
	for (const k of ['1:cc:34', '1:note:23', '1:note:26', '5:note:71', '5:cc:2', '7:note:101']) {
		assert.ok(seen.has(k), `expected a hint on ${k}`);
	}
	for (const s of [...mapModule.DDJ_SB_MAP.bindings, ...mapModule.DDJ_SB_MAP.hints]) {
		assert.notEqual(s.source.ch, 16, `${s.label ?? 'binding'} uses a channel-16 placeholder`);
	}
});

test('LED rules drive only sources the map binds ([PDF] MIDI-OUT rows of bound controls)', () => {
	const boundNotes = new Set(
		mapModule.DDJ_SB_MAP.bindings.filter((b) => b.source.kind === 'note').map((b) => key(b.source))
	);
	const leds = mapModule.DDJ_SB_MAP.leds;
	assert.ok(
		leds.length >= 2 * (4 + 3 + 8),
		`play/cue/sync/ch cue + 3 modes + 8 pad LEDs per deck, got ${leds.length}`
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
			kinds.has('hot_cue_present') &&
			kinds.has('pad_mode_selected')
	);
});

test('the registry carries DDJ_SB_MAP', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(mapModule.DDJ_SB_MAP));
	assert.equal(
		registry.DEVICE_MAP_REGISTRY.filter((m) => m.nameMatch === 'DDJ-SB(?:$|[^0-9])').length,
		1
	);
});
