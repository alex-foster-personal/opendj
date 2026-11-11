// Unit tests for the Hercules DJ Control Instinct live device map
// (src/lib/rb/midi/maps/hercules-dj-control-instinct.ts) + its entry in
// the maps/index.ts registry. Same vite+svelte bootstrap as ddj400-map.test.mjs.
//
// Every expected wire number below is read off the manufacturer's
// DJ Control Instinct technical-specifications MIDI table
// (https://ts.hercules.com/download/sound/manuals/DJ_Instinct/QSG/DJCInstinct_Technical_specifications.pdf),
// MIDI input / MIDI output sections. Nothing here is sourced from Mixxx or
// from tools/deck-diagrams/devices/hercules-dj-control-instinct/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap throws then broken (dupes/invalid)
//   if DEVICE_MAP_ENTRIES is not the live HERCULES_DJ_CONTROL_INSTINCT_MAP key then broken
//   if nameMatch matches Instinct P8, Reloop Mixtour or DDJ-400 then broken
//   if play/cue/sync/faders/crossfader/jogs drift from the chart then broken
//   if FX_KP pads are not color_fx filter|space|echo|next on notes 1-4 and 27-30 then broken
//   if LOAD is not notes 25/51 or hot cues are not 13-16 and 39-42 then broken
//   if any deck-3 or deck-4 binding exists then broken
//   if a binding's (kind, id) is absent from the manufacturer table then broken
//   if hints collide with bound sources then broken
//   if LED on/off is not Instinct polarity (0x00 / 0x7f) then broken
//   if nativeAudioProfile is not master12-cue34 then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-control-instinct.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

function findBindings(pred) {
	return mapModule.HERCULES_DJ_CONTROL_INSTINCT_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const CHART_NOTES = new Set([
	1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
	28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51,
	52, 53, 54, 55, 56, 57, 64, 65
]);
const CHART_CCS = new Set([48, 49, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62]);

function sourceKey(source) {
	return `${source.ch}:${source.kind}:${source.id}`;
}

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.HERCULES_DJ_CONTROL_INSTINCT_MAP);
	webmidi._resetMidiForTests();
});

test('DEVICE_MAP_ENTRIES is the live Instinct map and the registry includes it', () => {
	assert.deepEqual(
		mapModule.DEVICE_MAP_ENTRIES.map((e) => ({ key: e.key, live: e.live })),
		[{ key: 'HERCULES_DJ_CONTROL_INSTINCT_MAP', live: true }]
	);
	assert.equal(mapModule.DEVICE_MAP_ENTRIES[0].map, mapModule.HERCULES_DJ_CONTROL_INSTINCT_MAP);
	assert.ok(
		registry.DEVICE_MAP_REGISTRY.includes(mapModule.HERCULES_DJ_CONTROL_INSTINCT_MAP),
		'live Instinct map missing from DEVICE_MAP_REGISTRY'
	);
});

test('nameMatch covers Instinct and Instinct S, rejects P8 and other controllers', () => {
	const re = new RegExp(mapModule.HERCULES_DJ_CONTROL_INSTINCT_MAP.nameMatch, 'i');
	assert.ok(re.test('Hercules DJ Control Instinct'));
	assert.ok(re.test('DJ Control Instinct'));
	assert.ok(re.test('DJ Control Instinct S'));
	assert.ok(re.test('DJ Control Instinct MIDI 1'));
	assert.ok(!re.test('DJ Control Instinct P8'));
	assert.ok(!re.test('DJControl Instinct P8'));
	assert.ok(!re.test('Reloop Mixtour'));
	assert.ok(!re.test('DDJ-400'));
});

test('play/cue/sync, both faders, crossfader and both jogs match the chart', () => {
	const play1 = theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === 1);
	assert.deepEqual(play1.source, { ch: 1, kind: 'note', id: 22 });
	const play2 = theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === 2);
	assert.deepEqual(play2.source, { ch: 1, kind: 'note', id: 48 });
	const cue1 = theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === 1);
	assert.deepEqual(cue1.source, { ch: 1, kind: 'note', id: 21 });
	const cue2 = theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === 2);
	assert.deepEqual(cue2.source, { ch: 1, kind: 'note', id: 47 });
	const sync1 = theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === 1);
	assert.deepEqual(sync1.source, { ch: 1, kind: 'note', id: 23 });
	const sync2 = theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === 2);
	assert.deepEqual(sync2.source, { ch: 1, kind: 'note', id: 49 });
	const fader1 = theBinding(
		(b) => b.action.type === 'mixer_channel' && b.action.deck === 1 && b.action.target === 'fader'
	);
	assert.deepEqual(fader1.source, { ch: 1, kind: 'cc', id: 54 });
	const fader2 = theBinding(
		(b) => b.action.type === 'mixer_channel' && b.action.deck === 2 && b.action.target === 'fader'
	);
	assert.deepEqual(fader2.source, { ch: 1, kind: 'cc', id: 59 });
	const xf = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(xf.source, { ch: 1, kind: 'cc', id: 58 });
	assert.notEqual(xf.invert, true);
	for (const deck of [1, 2]) {
		const jog = theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.deepEqual(jog.source, { ch: 1, kind: 'cc', id: deck === 1 ? 48 : 49 });
		assert.equal(jog.relative, true);
		assert.equal(jog.relativeCentered, true);
		assert.equal(jog.invert, true);
	}
});

test('FX pads bind color_fx filter|space|echo|next on notes 1-4 and 27-30', () => {
	const fx = findBindings((b) => b.action.type === 'color_fx');
	assert.equal(fx.length, 8);
	const effects = ['filter', 'space', 'echo', 'next'];
	for (const [i, effect] of effects.entries()) {
		const d1 = theBinding(
			(b) => b.action.type === 'color_fx' && b.action.deck === 1 && b.action.effect === effect
		);
		assert.deepEqual(d1.source, { ch: 1, kind: 'note', id: 1 + i });
		const d2 = theBinding(
			(b) => b.action.type === 'color_fx' && b.action.deck === 2 && b.action.effect === effect
		);
		assert.deepEqual(d2.source, { ch: 1, kind: 'note', id: 27 + i });
	}
});

test('LOAD notes 25/51, hot cues 13-16 and 39-42, no deck above 2', () => {
	const load1 = theBinding((b) => b.action.type === 'browse_load' && b.action.deck === 1);
	assert.deepEqual(load1.source, { ch: 1, kind: 'note', id: 25 });
	const load2 = theBinding((b) => b.action.type === 'browse_load' && b.action.deck === 2);
	assert.deepEqual(load2.source, { ch: 1, kind: 'note', id: 51 });
	const slots = ['A', 'B', 'C', 'D'];
	for (const [i, slot] of slots.entries()) {
		const d1 = theBinding(
			(b) => b.action.type === 'deck_hot_cue' && b.action.deck === 1 && b.action.slot === slot
		);
		assert.deepEqual(d1.source, { ch: 1, kind: 'note', id: 13 + i });
		const d2 = theBinding(
			(b) => b.action.type === 'deck_hot_cue' && b.action.deck === 2 && b.action.slot === slot
		);
		assert.deepEqual(d2.source, { ch: 1, kind: 'note', id: 39 + i });
	}
	const strays = findBindings((b) => 'deck' in b.action && b.action.deck > 2);
	assert.deepEqual(strays, []);
});

test('every binding source is a manufacturer-chart (kind, id) on channel 1', () => {
	for (const b of mapModule.HERCULES_DJ_CONTROL_INSTINCT_MAP.bindings) {
		assert.equal(b.source.ch, 1, `binding ${JSON.stringify(b.action)} is not channel 1`);
		if (b.source.kind === 'note') {
			assert.ok(CHART_NOTES.has(b.source.id), `note ${b.source.id} is not in the Instinct chart`);
		} else if (b.source.kind === 'cc') {
			assert.ok(CHART_CCS.has(b.source.id), `cc ${b.source.id} is not in the Instinct chart`);
		} else {
			assert.fail(`unexpected kind ${b.source.kind}`);
		}
	}
});

test('hints never share a source with a binding', () => {
	const bound = new Set(
		mapModule.HERCULES_DJ_CONTROL_INSTINCT_MAP.bindings.map((b) => sourceKey(b.source))
	);
	const seen = new Set();
	for (const hint of mapModule.HERCULES_DJ_CONTROL_INSTINCT_MAP.hints) {
		const k = sourceKey(hint.source);
		assert.ok(!bound.has(k), `hint ${hint.label} shadows a bound source ${k}`);
		assert.ok(!seen.has(k), `duplicate hint source ${k}`);
		seen.add(k);
		assert.equal(hint.source.ch, 1);
		if (hint.source.kind === 'note') assert.ok(CHART_NOTES.has(hint.source.id));
		else assert.ok(CHART_CCS.has(hint.source.id));
	}
});

test('LED rules use Instinct polarity 0x00 on / 0x7f off', () => {
	const leds = mapModule.HERCULES_DJ_CONTROL_INSTINCT_MAP.leds;
	assert.ok(leds.length > 0);
	for (const rule of leds) {
		assert.equal(rule.out.ch, 1);
		assert.equal(rule.out.velocityOn, 0x00);
		assert.equal(rule.out.velocityOff, 0x7f);
	}
});

test('nativeAudioProfile is master12-cue34', () => {
	assert.equal(mapModule.HERCULES_DJ_CONTROL_INSTINCT_MAP.nativeAudioProfile, 'master12-cue34');
});
