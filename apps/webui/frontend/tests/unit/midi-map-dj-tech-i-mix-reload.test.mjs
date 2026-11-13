// requirement: MIDI-14457, MIDI-14543
// Unit tests for the DJ-Tech i-Mix Reload live device map
// (src/lib/rb/midi/maps/dj-tech-i-mix-reload.ts) + its entry in the maps registry.
//
// Every expected wire number is read off the DJ-Tech i-Mix Reload User Manual
// MIDI MAP. Nothing here is sourced from Mixxx or
// tools/deck-diagrams/devices/dj-tech-i-mix-reload/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(DJ_TECH_I_MIX_RELOAD_MAP) throws then broken ([MIDI MAP])
//   if nameMatch misses USB/MIDI suffix ports or matches MKII variants, Club Edition, bare i-Mix, sibling DJ-Tech maps, look-arounds, or fails resolveMapForPort then broken ([MIDI MAP])
//   if play/cue/sync per deck drift from KEY15/14/13 and KEY36/35/34 then broken ([MIDI MAP])
//   if channel faders are not VR14/VR15 or crossfader not VR16 then broken ([MIDI MAP])
//   if jog CW/CCW notes are not deck_tempo_nudge or any deck_jog exists then broken ([MIDI MAP])
//   if FX X knobs VR1/VR3 are not mixer_channel filter then broken ([MIDI MAP])
//   if LOAD A/B (KEY42/KEY43) are not browse_load then broken ([MIDI MAP])
//   if any binding addresses deck 3 or 4 then broken ([MIDI MAP])
//   if hints collide with bindings then broken ([MIDI MAP])
//   if DEVICE_MAP_REGISTRY lacks this map then broken ([MIDI MAP])
//   if any binding or hint source is not a MIDI MAP hex then broken ([MIDI MAP])
//   if the live map is absent from device-maps.json then broken
//   if device-maps.json differs from export script output then broken

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

// [MIDI MAP] every printed Note hex (KEY1-KEY43, ENC1, ENC1_KEY, ENC2, ENC3).
const CHART_NOTES = [
	0x04, 0x0a, 0x10, 0x16, 0x08, 0x0e, 0x14, 0x01, 0x07, 0x0d, 0x13, 0x0c, 0x05, 0x0b, 0x11, 0x17,
	0x15, 0x1b, 0x1c, 0x22, 0x18, 0x1e, 0x12, 0x24, 0x20, 0x26, 0x19, 0x1f, 0x25, 0x2b, 0x2a, 0x1d,
	0x23, 0x29, 0x2f, 0x03, 0x02, 0x21, 0x2d, 0x28, 0x2e, 0x1a, 0x64, 0x65, 0x27, 0x60, 0x61, 0x70,
	0x71, 0x72, 0x73, 0x62, 0x63, 0x74, 0x75, 0x76, 0x77
];
// [MIDI MAP] every printed CTRL hex (VR1-VR19 and CENTER twins).
const CHART_CCS = [
	0x44, 0x40, 0x46, 0x4c, 0x45, 0x66, 0x47, 0x51, 0x4e, 0x68, 0x50, 0x6b, 0x4f, 0x69, 0x48, 0x6c,
	0x4d, 0x6a, 0x52, 0x6d, 0x43, 0x42, 0x41, 0x4a, 0x49, 0x4b, 0x67
];
const CHART = new Set([
	...CHART_NOTES.map((id) => `1:note:${id}`),
	...CHART_CCS.map((id) => `1:cc:${id}`)
]);

let vite;
let webmidi;
let mapModule;
let registry;

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/dj-tech-i-mix-reload.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const MAP = () => mapModule.DJ_TECH_I_MIX_RELOAD_MAP;

/** Return the single binding matching pred, failing if there is not exactly one. */
function theBinding(pred) {
	const hits = MAP().bindings.filter(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const note = (id) => ({ ch: 1, kind: 'note', id });
const cc = (id) => ({ ch: 1, kind: 'cc', id });

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(MAP());
	webmidi._resetMidiForTests();
});

test('nameMatch covers i-Mix Reload ports and refuses MKII, club and other maps', () => {
	const { nameMatch } = MAP();
	const re = new RegExp(nameMatch, 'i');
	for (const name of [
		'DJ-Tech i-Mix Reload',
		'DJ-Tech i-Mix Reload MIDI 1',
		'i-Mix Reload',
		'i-Mix Reload MIDI 1',
		'DJ-Tech i-Mix Reload MIDI',
		'DJ-Tech i-Mix Reload 1',
		'i-Mix Reload 2'
	]) {
		assert.ok(re.test(name), name);
	}
	for (const name of [
		'i-Mix Reload MKII',
		'i-Mix Reload MK-II',
		'DJ-Tech i-Mix Reload MKII',
		'DJ-Tech i-Mix Reload MK-II',
		'i-Mix Reload MK2',
		'i-Mix Reload MK II',
		'DJ-Tech i-Mix Reload Club Edition',
		'i-Mix Club Edition',
		'DJ-Tech i-Mix',
		'i-Mix',
		'Mixtour',
		'DDJ-FLX4',
		'DJ-Tech Mix-101',
		'MIX-101'
	]) {
		assert.ok(!re.test(name), name);
	}
	for (const bad of ['(?=', '(?!', '(?<=', '(?<!']) {
		assert.ok(!nameMatch.includes(bad), `nameMatch must not contain ${bad}`);
	}
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	for (const name of ['DJ-Tech i-Mix Reload', 'DJ-Tech i-Mix Reload MIDI 1', 'i-Mix Reload']) {
		assert.equal(webmidi.resolveMapForPort(name), MAP(), name);
	}
	assert.notEqual(webmidi.resolveMapForPort('i-Mix Reload MKII'), MAP());
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('play / cue / sync per deck equal the MIDI MAP transport keys', () => {
	const rows = { 1: [0x11, 0x0b, 0x05], 2: [0x2f, 0x29, 0x23] };
	for (const deck of [1, 2]) {
		const [play, cue, sync] = rows[deck];
		const at = (type) => theBinding((b) => b.action.type === type && b.action.deck === deck).source;
		assert.deepEqual(at('deck_play_toggle'), note(play));
		assert.deepEqual(at('deck_cue'), note(cue));
		assert.deepEqual(at('deck_sync_toggle'), note(sync));
	}
});

test('channel faders VR14/VR15 and crossfader VR16 equal the MIDI MAP CCs', () => {
	const fader = (deck) =>
		theBinding(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.target === 'fader' && b.action.deck === deck
		).source;
	assert.deepEqual(fader(1), cc(0x43));
	assert.deepEqual(fader(2), cc(0x42));
	const xf = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(xf.source, cc(0x41));
});

test('jog touch-ON CW/CCW notes bind deck_tempo_nudge and nothing binds deck_jog', () => {
	const rows = { 1: [0x60, 0x61], 2: [0x62, 0x63] };
	for (const deck of [1, 2]) {
		const [cw, ccw] = rows[deck];
		const nudge = (dir) =>
			theBinding(
				(b) =>
					b.action.type === 'deck_tempo_nudge' &&
					b.action.deck === deck &&
					b.action.direction === dir
			).source;
		assert.deepEqual(nudge(1), note(cw));
		assert.deepEqual(nudge(-1), note(ccw));
	}
	assert.deepEqual(
		MAP().bindings.filter((b) => b.action.type === 'deck_jog'),
		[]
	);
});

test('FX X knobs VR1/VR3 bind mixer_channel filter per deck', () => {
	const filter = (deck) =>
		theBinding(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.target === 'filter' && b.action.deck === deck
		).source;
	assert.deepEqual(filter(1), cc(0x44));
	assert.deepEqual(filter(2), cc(0x46));
});

test('LOAD A / LOAD B notes bind browse_load', () => {
	const load = (deck) =>
		theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck).source;
	assert.deepEqual(load(1), note(0x2e));
	assert.deepEqual(load(2), note(0x1a));
});

test('two-deck controller: nothing addresses deck 3 or deck 4', () => {
	assert.deepEqual(
		MAP().bindings.filter((b) => 'deck' in b.action && b.action.deck > 2),
		[]
	);
});

test('hints do not collide with bindings', () => {
	const bound = new Set(MAP().bindings.map((b) => key(b.source)));
	assert.deepEqual(
		MAP().hints.filter((h) => bound.has(key(h.source))),
		[]
	);
});

test('every binding, hint and LED wire is a MIDI MAP hex on channel 1', () => {
	const sources = [
		...MAP().bindings.map((b) => b.source),
		...MAP().hints.map((h) => h.source),
		...MAP().leds.map((l) => note(l.out.note))
	];
	assert.deepEqual(
		sources.map(key).filter((k) => !CHART.has(k)),
		[]
	);
});

test('DEVICE_MAP_REGISTRY carries the live map', () => {
	assert.deepEqual(
		mapModule.DEVICE_MAP_ENTRIES.map((e) => [e.key, e.live]),
		[['DJ_TECH_I_MIX_RELOAD_MAP', true]]
	);
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(MAP()));
});

test('the engine export carries the live i-Mix Reload map', () => {
	const expected = JSON.parse(JSON.stringify(MAP()));
	const raw = readFileSync(
		resolve(FRONTEND_ROOT, '../../audio-engine/maps/device-maps.json'),
		'utf8'
	);
	const { maps } = JSON.parse(raw);
	assert.ok(Array.isArray(maps) && maps.length > 0, 'control: device-maps.json is non-empty');
	const hits = maps.filter((m) => m.nameMatch === expected.nameMatch);
	assert.equal(hits.length, 1, 'expected exactly one catalog slice for i-Mix Reload nameMatch');
	assert.deepEqual(hits[0], expected, 'engine catalog slice must match the live TypeScript map');
});

test('device-maps.json matches the export script output', () => {
	const run = spawnSync(process.execPath, ['scripts/export-midi-maps.mjs', '--check'], {
		cwd: FRONTEND_ROOT,
		encoding: 'utf8'
	});
	assert.equal(run.status, 0, String(run.stderr || run.stdout));
});
