// Unit tests for the Pioneer DJ DDJ-REV7 P0 device map
// (src/lib/rb/midi/maps/ddj-rev7.ts) + its entry in the maps/index.ts registry.
// Same vite+svelte bootstrap as ddj400-map.test.mjs.
//
// Every expected wire number is read off the official AlphaTheta
// "DDJ-REV7 List of MIDI message" PDF E1 ([PDF] fig rows; sha256 in
// docs/controller/research/DDJ-REV7-MIDI-Message-List.md). Nothing here is
// sourced from tools/deck-diagrams/devices/ddj-rev7/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(REV7_MAP) throws then broken
//   if nameMatch matches DDJ-REV1, DDJ-REV5 or DDJ-FLX4 then broken
//   if START/STOP isn't ch n note 0x0B -> deck_play_toggle ([PDF] D24) then broken
//   if tempo isn't cc 0 with lsbOffset 32 ([PDF] D11) then broken
//   if JOG isn't a centred relative cc 0x22 -> deck_jog ([PDF] D3) then broken
//   if AUTO LOOP / LOOP 1/2X / LOOP 2X / SYNC / KEY -/+ drift from 0x14/0x12/0x13/0x58/0x1F/0x1C then broken
//   if hot-cue pads aren't notes 0-7 on ch 8/10 -> slots A-H then broken
//   if any pad note above 7 is bound (SAVED FLIP is not a beat loop) then broken
//   if trim/ISO/fader MSB CCs drift from 4/7/11/15/19 ([PDF] M4-M8, M11) then broken
//   if FILTER isn't ch 7 CC 23/24 ([PDF] M5) then broken
//   if crossfader/master aren't ch7 CC 31/8 ([PDF] M12/M3) then broken
//   if browse isn't relative cc 0x40 (+SHIFT 0x64) and LOAD notes 0x46/0x47 then broken
//   if SHIFT isn't exactly one ch 7 note 0x3F shift_modifier ([PDF] M10) then broken
//   if deck_cue, channel_cue or master_cue is bound (the REV7 has no such button) then broken
//   if any deck-3 or deck-4 binding exists then broken
//   if hints collide with bound sources then broken
//   if a LED rule targets a row without a printed MIDI-OUT column then broken

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
		resolve: { alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') }, conditions: ['browser'] }
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-rev7.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const DECKS = [1, 2];
const PAD_CH = { 1: 8, 2: 10 }; // [PDF] channel assignment table
const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return mapModule.REV7_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

test('map registers cleanly and sits in the registry', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.REV7_MAP);
	webmidi._resetMidiForTests();
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(mapModule.REV7_MAP));
});

test('nameMatch covers DDJ-REV7 port names only', () => {
	const re = new RegExp(mapModule.REV7_MAP.nameMatch, 'i');
	assert.ok(re.test('DDJ-REV7'));
	assert.ok(re.test('Pioneer DJ DDJ-REV7 MIDI 1'));
	assert.ok(!re.test('DDJ-REV1'));
	assert.ok(!re.test('DDJ-REV5'));
	assert.ok(!re.test('DDJ-FLX4'));
});

test('transport, tempo and jog per deck ([PDF] D24, D11, D3)', () => {
	for (const deck of DECKS) {
		const play = theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck);
		assert.deepEqual(play.source, { ch: deck, kind: 'note', id: 0x0b });
		const pitch = theBinding((b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
		assert.deepEqual(pitch.source, { ch: deck, kind: 'cc', id: 0x00 });
		assert.equal(pitch.action.lsbOffset, 32);
		const jog = theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.deepEqual(jog.source, { ch: deck, kind: 'cc', id: 0x22 });
		assert.equal(jog.relative, true);
		assert.equal(jog.relativeCentered, true);
	}
});

test('loop, sync and key rows per deck ([PDF] D4-D6, D10, D14, D15)', () => {
	for (const deck of DECKS) {
		const on = (type, extra = {}) =>
			theBinding(
				(b) =>
					b.action.type === type &&
					b.action.deck === deck &&
					Object.entries(extra).every(([k, v]) => b.action[k] === v)
			).source.id;
		assert.equal(on('deck_auto_loop_toggle'), 0x14);
		assert.equal(on('deck_loop_scale', { factor: 0.5 }), 0x12);
		assert.equal(on('deck_loop_scale', { factor: 2 }), 0x13);
		assert.equal(on('deck_sync_toggle'), 0x58);
		assert.equal(on('deck_key_nudge', { semitones: -1 }), 0x1f);
		assert.equal(on('deck_key_nudge', { semitones: 1 }), 0x1c);
	}
});

test('hot-cue pads: notes 0-7 on the pad channel, nothing above ([PDF] P5-P12)', () => {
	const slots = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
	for (const deck of DECKS) {
		slots.forEach((slot, pad) => {
			const b = theBinding(
				(x) => x.action.type === 'deck_hot_cue' && x.action.deck === deck && x.action.slot === slot
			);
			assert.deepEqual(b.source, { ch: PAD_CH[deck], kind: 'note', id: pad });
		});
		const high = findBindings((b) => b.source.ch === PAD_CH[deck] && b.source.id > 7);
		assert.deepEqual(high, [], 'SAVED FLIP and other pad modes stay unbound');
	}
	assert.deepEqual(
		findBindings((b) => b.action.type === 'deck_beat_loop'),
		[]
	);
});

test('mixer strips: TRIM/ISO/FADER on the deck channel, FILTER on ch 7 ([PDF] M4-M8, M11, M5)', () => {
	const want = { trim: 0x04, fader: 0x13 };
	const eq = { high: 0x07, mid: 0x0b, low: 0x0f };
	for (const deck of DECKS) {
		for (const [target, cc] of Object.entries(want)) {
			const b = theBinding(
				(x) =>
					x.action.type === 'mixer_channel' && x.action.deck === deck && x.action.target === target
			);
			assert.deepEqual(b.source, { ch: deck, kind: 'cc', id: cc });
		}
		for (const [band, cc] of Object.entries(eq)) {
			const b = theBinding(
				(x) =>
					x.action.type === 'mixer_channel' &&
					x.action.deck === deck &&
					x.action.target === 'eq' &&
					x.action.band === band
			);
			assert.deepEqual(b.source, { ch: deck, kind: 'cc', id: cc });
		}
		const filter = theBinding(
			(x) =>
				x.action.type === 'mixer_channel' && x.action.deck === deck && x.action.target === 'filter'
		);
		assert.deepEqual(filter.source, { ch: 7, kind: 'cc', id: deck === 1 ? 0x17 : 0x18 });
	}
});

test('global channel 7: crossfader, master, phones, browse, load, shift ([PDF] M12, M3, M14, M15, B1, B3, M10)', () => {
	const g = (pred) => theBinding((b) => b.source.ch === 7 && pred(b));
	assert.equal(g((b) => b.action.target === 'crossfader').source.id, 0x1f);
	assert.equal(g((b) => b.action.target === 'master').source.id, 0x08);
	assert.equal(g((b) => b.action.type === 'headphone_mix').source.id, 0x0c);
	assert.equal(g((b) => b.action.type === 'headphone_level').source.id, 0x0d);
	const browse = findBindings((b) => b.action.type === 'browse_encoder');
	assert.deepEqual(
		browse.map((b) => b.source.id).sort((a, b) => a - b),
		[0x40, 0x64]
	);
	for (const b of browse) assert.equal(b.relative, true);
	assert.equal(g((b) => b.action.type === 'browse_load' && b.action.deck === 1).source.id, 0x46);
	assert.equal(g((b) => b.action.type === 'browse_load' && b.action.deck === 2).source.id, 0x47);
	const shifts = findBindings((b) => b.action.type === 'shift_modifier');
	assert.equal(shifts.length, 1, 'one hardware SHIFT, shared by both decks');
	assert.deepEqual(shifts[0].source, { ch: 7, kind: 'note', id: 0x3f });
});

test('no CUE, CH CUE or MASTER CUE, and nothing addresses deck 3 or 4', () => {
	for (const type of ['deck_cue', 'channel_cue', 'master_cue']) {
		assert.deepEqual(
			findBindings((b) => b.action.type === type),
			[],
			type
		);
	}
	assert.deepEqual(
		findBindings((b) => 'deck' in b.action && b.action.deck > 2),
		[]
	);
});

test('hints never collide with bound sources, and name the shift rows the replica needs', () => {
	const bound = new Set(mapModule.REV7_MAP.bindings.map((b) => key(b.source)));
	const hinted = new Set();
	for (const h of mapModule.REV7_MAP.hints) {
		assert.ok(!bound.has(key(h.source)), `hint ${h.label} shadows a binding`);
		assert.ok(!hinted.has(key(h.source)), `duplicate hint source ${h.label}`);
		hinted.add(key(h.source));
	}
	// [PDF] D4 AUTO LOOP + SHIFT note 80, D24 START/STOP + SHIFT note 71, B2 BACK note 101.
	assert.ok(hinted.has('1:note:80'));
	assert.ok(hinted.has('2:note:71'));
	assert.ok(hinted.has('7:note:101'));
});

test('LED rules target only rows with a printed MIDI-OUT column', () => {
	// [PDF] D24 START/STOP 0x0B, D4 AUTO LOOP 0x14, D10 SYNC 0x58 (deck channel); pads 0-7 (pad channel).
	const allowed = new Set();
	for (const deck of DECKS) {
		for (const note of [0x0b, 0x14, 0x58]) allowed.add(`${deck}:${note}`);
		for (let pad = 0; pad < 8; pad++) allowed.add(`${PAD_CH[deck]}:${pad}`);
	}
	assert.ok(mapModule.REV7_MAP.leds.length >= 22);
	for (const rule of mapModule.REV7_MAP.leds) {
		assert.ok(
			allowed.has(`${rule.out.ch}:${rule.out.note}`),
			`LED on ${rule.out.ch}:${rule.out.note}`
		);
	}
	const playing = mapModule.REV7_MAP.leds.filter((r) => r.trigger.kind === 'deck_playing');
	assert.deepEqual(
		playing.map((r) => r.out.note),
		[0x0b, 0x0b]
	);
});
