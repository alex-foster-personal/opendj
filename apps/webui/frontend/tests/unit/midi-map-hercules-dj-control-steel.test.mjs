// requirement: MIDI-11985
// Unit tests for the Hercules DJ Control Steel live app MIDI map
// (src/lib/rb/midi/maps/hercules-dj-control-steel.ts) + registry entry.
//
// Every expected wire number is read off the Hercules DJ Control Steel
// English user manual section 9 pages 20-22 (messages sent):
// https://ts.hercules.com/download/sound/manuals/DJSteel/01_DJ%20Control%20Steel_Eng.pdf
// Nothing here is sourced from Mixxx or from
// tools/deck-diagrams/devices/hercules-dj-control-steel/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(HERCULES_DJ_CONTROL_STEEL_MAP) throws then broken
//   if nameMatch misses Hercules DJ Control Steel or hits Compact/RMX then broken
//   if play/cue/sync/faders/xfader/jogs drift from section 9 then broken
//   if a binding id is absent from the section 9 send table then broken
//   if FX Control 1/2 or Apply are bound rather than hinted then broken
//   if any deck-3 or deck-4 binding exists then broken
//   if hints collide with bound sources then broken
//   if a deck jog decodes 0x01 as anything but +1 or 0x7F as anything but -1 then broken
//   if browse encoder decodes 0x01/0x7F wrong then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-control-steel.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

function findBindings(pred) {
	return mapModule.HERCULES_DJ_CONTROL_STEEL_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

/** Delta the runtime emits for one CC data byte on a relative binding (same rule as _valueFor). */
function decodedDelta(b, raw) {
	const d =
		b.relativeCentered === true ? webmidi.decodeRelativeCentered(raw) : webmidi.decodeRelative(raw);
	const unit = b.relativeUnit === true && d !== 0 ? Math.sign(d) : d;
	return b.invert === true ? -unit : unit;
}

const DECKS = [1, 2];

/** [PDF] section 9 messages sent: every CC id the Steel emits on channel 1. */
const SECTION9_SEND_CC_IDS = new Set([
	0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x10,
	0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f, 0x20,
	0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2a, 0x2b, 0x2c, 0x2d, 0x2e, 0x2f, 0x30,
	0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x3b, 0x3c, 0x3d, 0x3e, 0x3f, 0x40,
	0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x6b, 0x6c,
	0x6d, 0x6e, 0x6f, 0x70, 0x71, 0x72, 0x73, 0x74
]);

const PLAY_CC = { 1: 0x0b, 2: 0x23 };
const CUE_CC = { 1: 0x0c, 2: 0x24 };
const SYNC_CC = { 1: 0x07, 2: 0x1f };
const FADER_CC = { 1: 0x32, 2: 0x3c };
const JOG_CC = { 1: 0x2f, 2: 0x30 };
const LOAD_CC = { 1: 0x12, 2: 0x16 };
const FX_CONTROL_IDS = [0x41, 0x42, 0x43, 0x44];
const APPLY_FX_IDS = [0x70, 0x71, 0x72];

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.HERCULES_DJ_CONTROL_STEEL_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers DJ Control Steel port names only ([PDF] USB / Traktor string)', () => {
	const re = new RegExp(mapModule.HERCULES_DJ_CONTROL_STEEL_MAP.nameMatch, 'i');
	assert.ok(re.test('Hercules DJ Control Steel'));
	assert.ok(re.test('Hercules DJ Control Steel MIDI'));
	assert.ok(!re.test('DJControl Compact'));
	assert.ok(!re.test('DJControl MIX'));
	assert.ok(!re.test('DJ Control Instinct'));
	assert.ok(!re.test('DJ Console RMX'));
	assert.ok(!re.test('DJControl Inpulse'));
	assert.ok(!re.test('DDJ-FLX4'));
});

test('play/cue/sync per deck are section 9 CCs with ccButton ([PDF] §9)', () => {
	for (const deck of DECKS) {
		const play = theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck);
		assert.deepEqual(play.source, { ch: 1, kind: 'cc', id: PLAY_CC[deck] });
		assert.equal(play.ccButton, true);
		const cue = theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.deepEqual(cue.source, { ch: 1, kind: 'cc', id: CUE_CC[deck] });
		assert.equal(cue.ccButton, true);
		const sync = theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck);
		assert.deepEqual(sync.source, { ch: 1, kind: 'cc', id: SYNC_CC[deck] });
		assert.equal(sync.ccButton, true);
	}
});

test('channel faders and crossfader are section 9 analog CCs ([PDF] §9)', () => {
	for (const deck of DECKS) {
		const fader = theBinding(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
		);
		assert.deepEqual(fader.source, { ch: 1, kind: 'cc', id: FADER_CC[deck] });
	}
	const xf = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(xf.source, { ch: 1, kind: 'cc', id: 0x39 });
});

test('jogs are relative CCs 0x2F / 0x30 and decode 0x01/0x7F ([PDF] §9 Incremental)', () => {
	for (const deck of DECKS) {
		const jog = theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.deepEqual(jog.source, { ch: 1, kind: 'cc', id: JOG_CC[deck] });
		assert.equal(jog.relative, true);
		assert.notEqual(jog.relativeCentered, true);
		assert.equal(decodedDelta(jog, 0x01), 1);
		assert.equal(decodedDelta(jog, 0x7f), -1);
	}
});

test('browse encoders decode 0x01/0x7F; loads on 0x12 / 0x16 ([PDF] §9 / §6.9)', () => {
	for (const id of [0x45, 0x46]) {
		const browse = theBinding((b) => b.action.type === 'browse_encoder' && b.source.id === id);
		assert.deepEqual(browse.source, { ch: 1, kind: 'cc', id });
		assert.equal(browse.relative, true);
		assert.notEqual(browse.relativeCentered, true);
		assert.equal(decodedDelta(browse, 0x01), 1);
		assert.equal(decodedDelta(browse, 0x7f), -1);
	}
	for (const deck of DECKS) {
		const load = theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck);
		assert.deepEqual(load.source, { ch: 1, kind: 'cc', id: LOAD_CC[deck] });
		assert.equal(load.ccButton, true);
	}
});

test('shift status CC 0x73 / 0x74 use ccButton shift_modifier ([PDF] §9)', () => {
	for (const id of [0x73, 0x74]) {
		const shift = theBinding((b) => b.action.type === 'shift_modifier' && b.source.id === id);
		assert.equal(shift.ccButton, true);
		assert.deepEqual(shift.source, { ch: 1, kind: 'cc', id });
	}
});

test('FX Control 1/2 and Apply are hints, not bindings ([PDF] §9 / §6.15 / §6.16)', () => {
	const map = mapModule.HERCULES_DJ_CONTROL_STEEL_MAP;
	for (const id of [...FX_CONTROL_IDS, ...APPLY_FX_IDS]) {
		assert.deepEqual(
			findBindings((b) => b.source.ch === 1 && b.source.kind === 'cc' && b.source.id === id),
			[],
			`CC 0x${id.toString(16)} must stay a hint`
		);
		assert.ok(
			map.hints.some((h) => h.source.ch === 1 && h.source.kind === 'cc' && h.source.id === id),
			`CC 0x${id.toString(16)} missing from hints`
		);
	}
	assert.ok(map.hints.some((h) => h.label === 'FX Control 1 (deck 1)'));
	assert.ok(map.hints.some((h) => h.label === 'Apply FX (deck 1)'));
});

test('every binding is channel-1 CC from the section 9 send table ([PDF] §9)', () => {
	for (const b of mapModule.HERCULES_DJ_CONTROL_STEEL_MAP.bindings) {
		assert.equal(b.source.ch, 1);
		assert.equal(b.source.kind, 'cc');
		assert.ok(
			SECTION9_SEND_CC_IDS.has(b.source.id),
			`binding id 0x${b.source.id.toString(16)} is not in section 9`
		);
	}
});

test('two-channel controller: nothing addresses deck 3 or deck 4', () => {
	const strays = findBindings((b) => 'deck' in b.action && b.action.deck > 2);
	assert.deepEqual(strays, []);
});

test('hints name real unbound controls and never shadow a bound source', () => {
	const key = (s) => `${s.ch}:${s.kind}:${s.id}`;
	const bound = new Set(mapModule.HERCULES_DJ_CONTROL_STEEL_MAP.bindings.map((b) => key(b.source)));
	for (const hint of mapModule.HERCULES_DJ_CONTROL_STEEL_MAP.hints) {
		assert.ok(!bound.has(key(hint.source)), `hint ${hint.label} shadows bound source`);
		assert.equal(hint.source.ch, 1);
		assert.equal(hint.source.kind, 'cc');
		assert.ok(SECTION9_SEND_CC_IDS.has(hint.source.id), `hint ${hint.label} id not in section 9`);
	}
});

test('DEVICE_MAP_ENTRIES is one live HERCULES_DJ_CONTROL_STEEL_MAP entry', () => {
	assert.deepEqual(mapModule.DEVICE_MAP_ENTRIES, [
		{
			key: 'HERCULES_DJ_CONTROL_STEEL_MAP',
			map: mapModule.HERCULES_DJ_CONTROL_STEEL_MAP,
			live: true
		}
	]);
});

test('registry includes DJ Control Steel after loading maps/index.ts', () => {
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	const names = registry.DEVICE_MAP_REGISTRY.map((m) => m.nameMatch);
	assert.ok(names.includes('DJ Control Steel'));
	registry.registerAllDeviceMaps();
	registry.registerAllDeviceMaps();
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});
