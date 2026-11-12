// requirement: MIDI-11946
// Unit tests for DJ TechTools Midifighter map (djtechtools-midi-fighter.ts).
//
// Regression lines:
//   if registerDeviceMap(DJTT_MIDI_FIGHTER_MAP) throws then broken
//   if a binding uses a wire outside notes 36-51 or CC 16-19 on ch 3 then broken
//   if play is not note 37 deck 1 / 39 deck 2 then broken
//   if channel faders are not CC 16/17 or crossfader not CC 18 then broken
//   if deck_jog or browse_encoder appears then broken
//   if nameMatch matches Midi Fighter Twister then broken
//   if cue keys 36/38 carry LED rules then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

const CHART_NOTES = new Set(Array.from({ length: 16 }, (_, i) => 36 + i));
const CHART_CCS = new Set([16, 17, 18, 19]);
const HINT_NOTES = new Set([52, 53, 54, 55]);
const HINT_CCS = new Set([20, 21, 22, 23]);

const GRID = [
	[48, 'browse_load', 1],
	[49, 'channel_cue', 1],
	[50, 'browse_load', 2],
	[51, 'channel_cue', 2],
	[44, 'deck_hot_cue', 1, 'A'],
	[45, 'deck_hot_cue', 1, 'B'],
	[46, 'deck_hot_cue', 2, 'A'],
	[47, 'deck_hot_cue', 2, 'B'],
	[40, 'deck_sync_toggle', 1],
	[41, 'deck_auto_loop_toggle', 1],
	[42, 'deck_sync_toggle', 2],
	[43, 'deck_auto_loop_toggle', 2],
	[36, 'deck_cue', 1],
	[37, 'deck_play_toggle', 1],
	[38, 'deck_cue', 2],
	[39, 'deck_play_toggle', 2]
];

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/djtechtools-midi-fighter.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const MAP = () => mapModule.DJTT_MIDI_FIGHTER_MAP;

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(MAP());
	webmidi._resetMidiForTests();
});

test('chart-only wires: bindings and hints are disjoint sets on ch 3', () => {
	const { bindings, hints } = MAP();
	for (const b of bindings) {
		assert.equal(b.source.ch, 3);
		if (b.source.kind === 'note') {
			assert.ok(CHART_NOTES.has(b.source.id), `note ${b.source.id} not in chart`);
		} else {
			assert.equal(b.source.kind, 'cc');
			assert.ok(CHART_CCS.has(b.source.id), `cc ${b.source.id} not in chart`);
		}
	}
	for (const h of hints) {
		assert.equal(h.source.ch, 3);
		if (h.source.kind === 'note') {
			assert.ok(HINT_NOTES.has(h.source.id));
		} else {
			assert.ok(HINT_CCS.has(h.source.id));
		}
	}
	const boundKeys = new Set(bindings.map((b) => `${b.source.kind}:${b.source.id}`));
	for (const h of hints) {
		assert.ok(!boundKeys.has(`${h.source.kind}:${h.source.id}`));
	}
	const noteBindings = bindings.filter((b) => b.source.kind === 'note');
	assert.equal(noteBindings.length, 16);
	for (let n = 36; n <= 51; n++) {
		const count = noteBindings.filter((b) => b.source.id === n).length;
		assert.equal(count, 1, `note ${n} bound count`);
	}
	const alien = { ch: 3, kind: 'note', id: 56 };
	assert.ok(!CHART_NOTES.has(alien.id));
	assert.ok(!HINT_NOTES.has(alien.id));
});

test('grid layout: exact note-to-action table', () => {
	for (const row of GRID) {
		const [note, type, deck, slot] = row;
		const b = MAP().bindings.find((x) => x.source.kind === 'note' && x.source.id === note);
		assert.ok(b, `missing binding for note ${note}`);
		assert.equal(b.action.type, type);
		assert.equal(b.action.deck, deck);
		if (slot !== undefined) {
			assert.equal(b.action.slot, slot);
		}
	}
});

test('required transport and mixer CCs', () => {
	const { bindings } = MAP();
	for (const deck of [1, 2]) {
		assert.ok(bindings.some((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck));
		assert.ok(bindings.some((b) => b.action.type === 'deck_cue' && b.action.deck === deck));
		assert.ok(bindings.some((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck));
	}
	const fader1 = bindings.find(
		(b) => b.action.type === 'mixer_channel' && b.action.deck === 1 && b.action.target === 'fader'
	);
	const fader2 = bindings.find(
		(b) => b.action.type === 'mixer_channel' && b.action.deck === 2 && b.action.target === 'fader'
	);
	const xf = bindings.find(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(fader1?.source, { ch: 3, kind: 'cc', id: 16 });
	assert.deepEqual(fader2?.source, { ch: 3, kind: 'cc', id: 17 });
	assert.deepEqual(xf?.source, { ch: 3, kind: 'cc', id: 18 });
	for (const b of bindings.filter((x) => x.source.kind === 'cc')) {
		assert.notEqual(b.relative, true);
	}
});

test('no jog, no encoder, no relative bindings', () => {
	for (const b of MAP().bindings) {
		assert.notEqual(b.action.type, 'deck_jog');
		assert.notEqual(b.action.type, 'browse_encoder');
		assert.notEqual(b.relative, true);
	}
});

test('nameMatch: Midifighter yes, later DJTT models no', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(MAP());
	const match = (name) => webmidi.resolveMapForPort(name) === MAP();
	assert.ok(match('Midifighter'));
	assert.ok(match('Midifighter MIDI 1'));
	const re = new RegExp(MAP().nameMatch, 'i');
	assert.ok(re.test('Midifighter'));
	assert.ok(!re.test('Midi Fighter Twister'));
	assert.ok(!re.test('Midi Fighter 3D'));
	assert.ok(!re.test('Midi Fighter Spectra'));
	assert.ok(!re.test('Midi Fighter 64'));
	assert.ok(!re.test('DDJ-FLX4'));
	assert.ok(!re.test('Mixtour'));
	webmidi._resetMidiForTests();
});

test('LED rules on bound keys, not on cue keys 36/38', () => {
	const { leds } = MAP();
	for (const rule of leds) {
		assert.equal(rule.out.ch, 3);
		assert.ok(rule.out.note >= 37 && rule.out.note <= 51);
		assert.equal(rule.out.velocityOn, 0x7f);
		assert.equal(rule.out.velocityOff, 0x00);
		const key = MAP().bindings.find(
			(b) => b.source.kind === 'note' && b.source.id === rule.out.note
		);
		assert.ok(key, `LED note ${rule.out.note} has no binding`);
	}
	assert.ok(!leds.some((r) => r.out.note === 36));
	assert.ok(!leds.some((r) => r.out.note === 38));
});

test('registry includes exactly one Midifighter map', () => {
	const midis = registry.DEVICE_MAP_REGISTRY.filter((m) => m.nameMatch === 'Midifighter');
	assert.equal(midis.length, 1);
	assert.ok(registry.DEVICE_MAP_REGISTRY.some((m) => m === MAP()));
});
