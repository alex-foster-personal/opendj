// requirement: MIDI-12036
// Every expected Send Number is from the Gemini CDMP-7000 manual page-19 MIDI DEVICE MESSAGES TABLE.
//
//   if registerDeviceMap throws then broken
//   if CDMP-7000 L ports do not resolve to this map then broken
//   if CDMP-7000 R resolves here then broken
//   if bindings drift from the 14 chart rows then broken
//   if play cue sync jog are missing then broken
//   if fader or crossfader bindings appear then broken
//   if FX ids are bound not hinted then broken
//   if hints collide with bindings then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

/** Page-19 table send numbers 1..44 (chart has no channel/kind column). */
const CHART_IDS = new Set(Array.from({ length: 44 }, (_, i) => i + 1));
const KIND_UNVERIFIABLE = new Set([23, 25, 26, 27, 37, 38, 39, 40, 42, 43, 44]);

const EXPECTED_HINT_KEYS = new Set(
	[
		'note:8',
		'note:9',
		'note:10',
		'note:11',
		'note:12',
		'note:14',
		'note:19',
		'note:20',
		'note:21',
		'note:22',
		'note:24',
		'note:28',
		'note:29',
		'note:30',
		'note:31',
		'note:34',
		'note:35',
		'note:36',
		'cc:41'
	].map((k) => `1:${k}`)
);

let vite;
let webmidi;
let mapModule;
let mapsIndex;

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/gemini-cdmp-7000-l-audio.ts');
	mapsIndex = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

function binding(kind, id) {
	return mapModule.GEMINI_CDMP7000_L_MAP.bindings.find(
		(b) => b.source.ch === 1 && b.source.kind === kind && b.source.id === id
	);
}

function sourceKey(s) {
	return `${s.ch}:${s.kind}:${s.id}`;
}

test('registers and resolves CDMP-7000 L port names only', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.GEMINI_CDMP7000_L_MAP);
	for (const name of ['CDMP-7000 L', 'CDMP-7000 L MIDI 1', 'Gemini CDMP-7000 L audio']) {
		assert.equal(
			webmidi.resolveMapForPort(name)?.nameMatch,
			mapModule.GEMINI_CDMP7000_L_MAP.nameMatch
		);
	}
	for (const name of ['CDMP-7000 R', 'CDMP-7000 R Audio']) {
		assert.equal(webmidi.resolveMapForPort(name), null);
	}
	webmidi._resetMidiForTests();
});

test('wins port resolution after every live registry map is registered', () => {
	webmidi._resetMidiForTests();
	for (const map of mapsIndex.DEVICE_MAP_REGISTRY) {
		webmidi.registerDeviceMap(map);
	}
	assert.equal(
		webmidi.resolveMapForPort('CDMP-7000 L')?.nameMatch,
		mapModule.GEMINI_CDMP7000_L_MAP.nameMatch
	);
	webmidi._resetMidiForTests();
});

test('exact fourteen bindings from page-19 table', () => {
	const expected = [
		['note', 1, { type: 'deck_cue', deck: 1 }],
		['note', 2, { type: 'deck_play_toggle', deck: 1 }],
		['note', 3, { type: 'deck_tempo_nudge', deck: 1, direction: 1 }],
		['note', 4, { type: 'deck_tempo_nudge', deck: 1, direction: -1 }],
		['note', 5, { type: 'deck_hot_cue', deck: 1, slot: 'A' }],
		['note', 6, { type: 'deck_hot_cue', deck: 1, slot: 'B' }],
		['note', 7, { type: 'deck_hot_cue', deck: 1, slot: 'C' }],
		['note', 13, { type: 'deck_sync_toggle', deck: 1 }],
		['note', 15, { type: 'browse_load', deck: 1 }],
		['note', 16, { type: 'deck_manual_loop_cycle', deck: 1 }],
		['note', 17, { type: 'deck_manual_loop_cycle', deck: 1 }],
		['note', 18, { type: 'deck_loop_exit', deck: 1 }],
		['cc', 32, { type: 'deck_jog', deck: 1 }],
		['cc', 33, { type: 'deck_pitch', deck: 1, lsbOffset: null }]
	];
	assert.equal(mapModule.GEMINI_CDMP7000_L_MAP.bindings.length, 14);
	for (const [kind, id, action] of expected) {
		const b = binding(kind, id);
		assert.ok(b, `missing binding ${kind} ${id}`);
		assert.deepEqual(b.action, action);
	}
	const jog = binding('cc', 32);
	assert.equal(jog.relative, true);
	assert.equal(jog.relativeCentered, true);
	const pitch = binding('cc', 33);
	assert.equal(pitch.invert, undefined);
});

test('every wire is ch1 chart id and skips kind-unverifiable ids', () => {
	for (const b of mapModule.GEMINI_CDMP7000_L_MAP.bindings) {
		assert.equal(b.source.ch, 1);
		assert.ok(CHART_IDS.has(b.source.id), `id ${b.source.id} not in chart`);
		assert.ok(!KIND_UNVERIFIABLE.has(b.source.id), `bound unverifiable id ${b.source.id}`);
	}
});

test('play cue sync jog bound; no analog-mixer faders; deck 1 only', () => {
	const types = new Set(mapModule.GEMINI_CDMP7000_L_MAP.bindings.map((b) => b.action.type));
	assert.ok(types.has('deck_play_toggle'));
	assert.ok(types.has('deck_cue'));
	assert.ok(types.has('deck_sync_toggle'));
	assert.ok(types.has('deck_jog'));
	const badMixer = mapModule.GEMINI_CDMP7000_L_MAP.bindings.filter(
		(b) =>
			b.action.type === 'mixer_channel' &&
			(b.action.target === 'fader' || b.action.target === 'filter')
	);
	assert.deepEqual(badMixer, []);
	const badGlobal = mapModule.GEMINI_CDMP7000_L_MAP.bindings.filter(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(badGlobal, []);
	const strayDecks = mapModule.GEMINI_CDMP7000_L_MAP.bindings.filter(
		(b) => 'deck' in b.action && b.action.deck !== 1
	);
	assert.deepEqual(strayDecks, []);
});

test('FX controls are hints not bindings', () => {
	for (const id of [20, 21, 22, 36]) {
		assert.equal(binding('note', id), undefined, `FX note ${id} must not bind`);
	}
	assert.equal(binding('cc', 41), undefined, 'DRY/WET must not bind');
	assert.ok(
		mapModule.GEMINI_CDMP7000_L_MAP.hints.some(
			(h) => h.source.kind === 'note' && h.source.id === 20
		)
	);
	assert.ok(
		mapModule.GEMINI_CDMP7000_L_MAP.hints.some((h) => h.source.kind === 'cc' && h.source.id === 41)
	);
	assert.equal(
		mapModule.GEMINI_CDMP7000_L_MAP.bindings.filter((b) => b.action.type === 'mixer_channel')
			.length,
		0
	);
});

test('hints match the exhaustive list and never shadow bindings', () => {
	const bound = new Set(mapModule.GEMINI_CDMP7000_L_MAP.bindings.map((b) => sourceKey(b.source)));
	const hintKeys = new Set();
	for (const h of mapModule.GEMINI_CDMP7000_L_MAP.hints) {
		assert.equal(h.source.ch, 1);
		assert.ok(!bound.has(sourceKey(h.source)), `hint shadows ${sourceKey(h.source)}`);
		hintKeys.add(sourceKey(h.source));
	}
	assert.deepEqual(hintKeys, EXPECTED_HINT_KEYS);
});

test('DEVICE_MAP_ENTRIES and registry include this map', () => {
	assert.deepEqual(mapModule.DEVICE_MAP_ENTRIES, [
		{ key: 'GEMINI_CDMP7000_L_MAP', map: mapModule.GEMINI_CDMP7000_L_MAP, live: true }
	]);
	const names = mapsIndex.DEVICE_MAP_REGISTRY.map((m) => m.nameMatch);
	assert.ok(names.includes('CDMP-7000 L'));
});
