// requirement: MIDI-11964
// Unit tests for the Hercules DJControl Compact P0 device map
// (src/lib/rb/midi/maps/hercules-djcontrol-compact.ts) + registry entry.
//
// Every expected wire number is read off the official Hercules
// DJControl Compact MIDI Mapping Version 1.1 PDF ([PDF] Midi Input / Midi Output).
// Nothing here is sourced from tools/deck-diagrams/devices/hercules-djcontrol-compact/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap throws then broken
//   if nameMatch misses Hercules DJControl Compact or hits Inpulse/MIX/Instinct then broken
//   if play/cue/sync are not ch1 notes 0x21/0x22/0x23 and 0x51/0x52/0x53 then broken
//   if either VOL or XFADER is missing then broken
//   if either jog is missing, not relative, or relativeCentered then broken
//   if any FX_KP pad is unbound then broken
//   if any source is not in the PDF Midi Input table then broken
//   if a deck-3/4 binding exists then broken
//   if a hint shadows a bound source then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-djcontrol-compact.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

function findBindings(pred) {
	return mapModule.HERCULES_DJCONTROL_COMPACT_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const DECKS = [1, 2];
const PLAY_NOTE = { 1: 0x21, 2: 0x51 };
const CUE_NOTE = { 1: 0x22, 2: 0x52 };
const SYNC_NOTE = { 1: 0x23, 2: 0x53 };
const VOL_CC = { 1: 0x39, 2: 0x3d };
const JOG_CC = { 1: 0x30, 2: 0x31 };
const FX_NOTE = { 1: 0x09, 2: 0x39 };
const CUE_PAD_NOTE = { 1: 0x01, 2: 0x31 };
const LOOP_PAD_NOTE = { 1: 0x19, 2: 0x49 };

function range(from, to) {
	const out = [];
	for (let i = from; i <= to; i++) out.push(i);
	return out;
}

/** [PDF] Midi Input note ids: 0x01–0x26, 0x2B–0x30, 0x31–0x56. */
const PDF_NOTES = new Set([...range(0x01, 0x26), ...range(0x2b, 0x30), ...range(0x31, 0x56)]);
/** [PDF] Midi Input CC ids. No 0x3A / 0x3E on this chart. */
const PDF_CCS = new Set([0x30, 0x31, 0x36, 0x37, 0x38, 0x39, 0x3b, 0x3c, 0x3d, 0x3f, 0x40]);

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.HERCULES_DJCONTROL_COMPACT_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers DJControl Compact port names only ([PDF] USB name)', () => {
	const re = new RegExp(mapModule.HERCULES_DJCONTROL_COMPACT_MAP.nameMatch, 'i');
	assert.ok(re.test('DJControl Compact'));
	assert.ok(re.test('Hercules DJControl Compact'));
	assert.ok(re.test('Hercules DJControl Compact MIDI'));
	assert.ok(!re.test('DJControl Inpulse 200'));
	assert.ok(!re.test('DJControl MIX'));
	assert.ok(!re.test('DJControl Instinct'));
	assert.ok(!re.test('DJControl Starlight'));
	assert.ok(!re.test('Mixtour'));
});

test('play/cue/sync per deck on ch 1 notes 0x21/0x22/0x23 and 0x51/0x52/0x53 ([PDF])', () => {
	for (const deck of DECKS) {
		const play = theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck);
		assert.deepEqual(play.source, { ch: 1, kind: 'note', id: PLAY_NOTE[deck] });
		const cue = theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.deepEqual(cue.source, { ch: 1, kind: 'note', id: CUE_NOTE[deck] });
		const sync = theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck);
		assert.deepEqual(sync.source, { ch: 1, kind: 'note', id: SYNC_NOTE[deck] });
	}
});

test('both channel faders and the crossfader are bound ([PDF] VOL_DA/VOL_DB/XFADER)', () => {
	for (const deck of DECKS) {
		const vol = theBinding(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
		);
		assert.deepEqual(vol.source, { ch: 1, kind: 'cc', id: VOL_CC[deck] });
	}
	const xf = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(xf.source, { ch: 1, kind: 'cc', id: 0x36 });
});

test("both jogs are relative two's-complement deck_jog, not centre-64 ([PDF] JOG_DA/JOG_DB)", () => {
	for (const deck of DECKS) {
		const jog = theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.deepEqual(jog.source, { ch: 1, kind: 'cc', id: JOG_CC[deck] });
		assert.equal(jog.relative, true);
		assert.ok(!jog.relativeCentered);
	}
});

test('FX_KP1-4 both decks bind controller_pad 1-4 unshifted ([PDF] FX_KP)', () => {
	for (const deck of DECKS) {
		for (const pad of [1, 2, 3, 4]) {
			const b = theBinding(
				(x) =>
					x.action.type === 'controller_pad' &&
					x.action.deck === deck &&
					x.action.pad === pad &&
					x.action.shifted === false
			);
			assert.deepEqual(b.source, { ch: 1, kind: 'note', id: FX_NOTE[deck] + pad - 1 });
		}
	}
});

test('CUE pads are hot cues A-D; LOOP pads use COMPACT_BEAT_LOOP_PAD_BEATS ([PDF])', () => {
	assert.deepEqual(mapModule.COMPACT_BEAT_LOOP_PAD_BEATS, [1, 2, 4, 8]);
	const slots = ['A', 'B', 'C', 'D'];
	for (const deck of DECKS) {
		for (let pad = 0; pad < 4; pad++) {
			const cue = theBinding(
				(x) =>
					x.action.type === 'deck_hot_cue' && x.action.deck === deck && x.action.slot === slots[pad]
			);
			assert.deepEqual(cue.source, { ch: 1, kind: 'note', id: CUE_PAD_NOTE[deck] + pad });
			const loop = theBinding(
				(x) =>
					x.action.type === 'deck_beat_loop' &&
					x.action.deck === deck &&
					x.action.beats === mapModule.COMPACT_BEAT_LOOP_PAD_BEATS[pad]
			);
			assert.deepEqual(loop.source, { ch: 1, kind: 'note', id: LOOP_PAD_NOTE[deck] + pad });
		}
	}
});

test('two-channel controller: nothing addresses deck 3 or deck 4', () => {
	const strays = findBindings((b) => 'deck' in b.action && b.action.deck > 2);
	assert.deepEqual(strays, []);
});

test('every binding and hint source is in the PDF Midi Input table on ch 1', () => {
	const map = mapModule.HERCULES_DJCONTROL_COMPACT_MAP;
	for (const row of [...map.bindings, ...map.hints]) {
		assert.equal(row.source.ch, 1, `${row.source.kind} ${row.source.id}`);
		const allowed = row.source.kind === 'note' ? PDF_NOTES : PDF_CCS;
		assert.ok(
			row.source.kind === 'note' || row.source.kind === 'cc',
			`unexpected kind ${row.source.kind}`
		);
		assert.ok(allowed.has(row.source.id), `${row.source.kind} 0x${row.source.id.toString(16)}`);
	}
});

test('hints name unbound controls and never shadow a bound source', () => {
	const key = (s) => `${s.ch}:${s.kind}:${s.id}`;
	const bound = new Set(
		mapModule.HERCULES_DJCONTROL_COMPACT_MAP.bindings.map((b) => key(b.source))
	);
	for (const hint of mapModule.HERCULES_DJCONTROL_COMPACT_MAP.hints) {
		assert.ok(!bound.has(key(hint.source)), `hint ${hint.label} shadows bound source`);
	}
});

test('registry includes DJControl Compact and registerAllDeviceMaps is idempotent', () => {
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	const names = registry.DEVICE_MAP_REGISTRY.map((m) => m.nameMatch);
	assert.ok(names.includes('DJControl Compact'));
	registry.registerAllDeviceMaps();
	registry.registerAllDeviceMaps();
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('init is Update All Controls Bx 7F ON ([PDF] Midi Output)', () => {
	assert.deepEqual(mapModule.HERCULES_DJCONTROL_COMPACT_MAP.init, [[0xb0, 0x7f, 0x7f]]);
});
