// requirement: MIDI-12050
// Unit tests for the Hercules DJControl Inpulse 500 live map
// (src/lib/rb/midi/maps/hercules-inpulse-500.ts).
//
// CHART is a literal transcription of DJControlInpulse500_MIDI_Commands.pdf hex columns
// for every row the map binds or hints (1-based channel = status nibble + 1).
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(HERCULES_INPULSE_500_MAP) throws then broken
//   if nameMatch misses Inpulse 500 or matches other Hercules decks then broken
//   if any binding or hint source is off-chart then broken
//   if play/cue/sync/faders/crossfader/jogs are missing or jogs use relativeCentered then broken
//   if Filter isn't mixer_channel filter or Fx1-4 are bound not hinted then broken
//   if binding/hint collide or deck_pitch lsbOffset isn't 32 then broken
//   if LED outs aren't chart MIDI OUT rows then broken
//   if registry lacks HERCULES_INPULSE_500_MAP or export --check fails then broken

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const DECKS = [1, 2];
const DECK_NOTE_IDS = [0x05, 0x06, 0x07, 0x09, 0x0a, 0x0c, 0x0d, 0x2c];
const DECK_CC_IDS = [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x08, 0x09, 0x0a, 0x0e];
const DECK_HINT_NOTE_IDS = [
	0x01, 0x02, 0x03, 0x04, 0x0b, 0x0e, 0x0f, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x40, 0x41,
	0x42, 0x43
];
const SHIFT_DECK_NOTE_IDS = [0x05, 0x06, 0x07, 0x0d];
const PAD_NOTE_IDS = [
	...Array.from({ length: 8 }, (_, i) => i),
	...Array.from({ length: 8 }, (_, i) => 0x08 + i)
];
const FX_NOTES = [0x14, 0x15, 0x16, 0x17];
const GLOBAL_CC = [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b];
const GLOBAL_NOTE = [0x00, 0x01, 0x02, 0x18, ...FX_NOTES];
const SHIFT_BROWSER_NOTE = [0x00, 0x02, ...FX_NOTES];

const CHART = new Set([
	...DECKS.flatMap((deck) => [
		...DECK_NOTE_IDS.map((id) => `${deck + 1}:note:${id}`),
		...DECK_CC_IDS.map((id) => `${deck + 1}:cc:${id}`),
		...(deck === 1 ? DECK_HINT_NOTE_IDS : DECK_HINT_NOTE_IDS.filter((id) => id !== 0x43)).map(
			(id) => `${deck + 1}:note:${id}`
		)
	]),
	...DECKS.flatMap((deck) => PAD_NOTE_IDS.map((id) => `${deck + 6}:note:${id}`)),
	...DECKS.flatMap((deck) => SHIFT_DECK_NOTE_IDS.map((id) => `${deck + 4}:note:${id}`)),
	...GLOBAL_CC.map((id) => `1:cc:${id}`),
	...GLOBAL_NOTE.map((id) => `1:note:${id}`),
	'4:cc:1',
	...SHIFT_BROWSER_NOTE.map((id) => `4:note:${id}`)
]);

let vite;
let webmidi;
let map;
let registry;

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;
const bindingsAt = (ch, kind, id) =>
	map.bindings.filter((b) => b.source.ch === ch && b.source.kind === kind && b.source.id === id);

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
	map = (await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-inpulse-500.ts'))
		.HERCULES_INPULSE_500_MAP;
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

test('HERCULES_INPULSE_500_MAP registers without throwing', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(map);
	webmidi._resetMidiForTests();
});

test('nameMatch matches Inpulse 500 USB names and rejects sibling models', () => {
	const re = new RegExp(map.nameMatch, 'i');
	assert.ok(re.test('DJControl Inpulse 500'));
	assert.ok(re.test('Hercules DJControl Inpulse 500'));
	assert.ok(re.test('DJControl Inpulse 500 MIDI 1'));
	assert.ok(!re.test('DJControl Inpulse 200'));
	assert.ok(!re.test('DJControl Inpulse 300'));
	assert.ok(!re.test('DJControl Inpulse 300 MK2'));
	assert.ok(!re.test('DJControl Mix'));
	assert.ok(!re.test('DJConsole RMX2'));
});

test('every binding source is a manufacturer chart row', () => {
	const offChart = map.bindings.map((b) => key(b.source)).filter((k) => !CHART.has(k));
	assert.deepEqual(offChart, []);
});

test('every hint source is a manufacturer chart row', () => {
	const offChart = (map.hints ?? []).map((h) => key(h.source)).filter((k) => !CHART.has(k));
	assert.deepEqual(offChart, []);
});

test('play, cue, sync, faders, crossfader and jogs are bound', () => {
	for (const deck of DECKS) {
		const ch = deck + 1;
		assert.ok(bindingsAt(ch, 'note', 0x07).some((b) => b.action.type === 'deck_play_toggle'));
		assert.ok(bindingsAt(ch, 'note', 0x06).some((b) => b.action.type === 'deck_cue'));
		assert.ok(bindingsAt(ch, 'note', 0x05).some((b) => b.action.type === 'deck_sync_toggle'));
		assert.ok(
			bindingsAt(ch, 'cc', 0x00).some(
				(b) => b.action.type === 'mixer_channel' && b.action.target === 'fader'
			)
		);
		for (const jogCc of [0x09, 0x0a]) {
			const jog = bindingsAt(ch, 'cc', jogCc).find((b) => b.action.type === 'deck_jog');
			assert.ok(jog, `jog cc ${jogCc} deck ${deck}`);
			assert.equal(jog.relative, true);
			assert.notEqual(jog.relativeCentered, true);
		}
	}
	assert.ok(
		bindingsAt(1, 'cc', 0x00).some(
			(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
		)
	);
});

test('Filter is colour FX; Fx1-4 are hints only', () => {
	for (const deck of DECKS) {
		const ch = deck + 1;
		assert.ok(
			bindingsAt(ch, 'cc', 0x01).some(
				(b) => b.action.type === 'mixer_channel' && b.action.target === 'filter'
			)
		);
	}
	for (const id of FX_NOTES) {
		assert.ok((map.hints ?? []).some((h) => key(h.source) === `1:note:${id}`));
		assert.equal(bindingsAt(1, 'note', id).length, 0);
	}
});

test('no binding/hint collision; pitch lsbOffset 32; no shift_modifier', () => {
	const bound = new Set(map.bindings.map((b) => key(b.source)));
	for (const h of map.hints ?? []) {
		assert.ok(!bound.has(key(h.source)), `hint collides: ${key(h.source)}`);
	}
	for (const deck of DECKS) {
		const pitch = bindingsAt(deck + 1, 'cc', 0x08).find((b) => b.action.type === 'deck_pitch');
		assert.ok(pitch);
		assert.equal(pitch.action.lsbOffset, 32);
	}
	assert.equal(map.bindings.filter((b) => b.action.type === 'shift_modifier').length, 0);
	assert.equal(map.bindings.filter((b) => b.action.deck === 3 || b.action.deck === 4).length, 0);
});

test('LED outs are chart MIDI OUT rows', () => {
	const ledKeys = map.leds.map((r) => `${r.out.ch}:note:${r.out.note}`);
	for (const k of ledKeys) {
		assert.ok(CHART.has(k), `LED out ${k} not in chart`);
	}
});

test('registry includes HERCULES_INPULSE_500_MAP', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(map));
	assert.equal(registry.DEVICE_MAP_REGISTRY.filter((m) => m.nameMatch === map.nameMatch).length, 1);
});

test('device-maps.json matches export script', () => {
	const r = spawnSync(process.execPath, ['scripts/export-midi-maps.mjs', '--check'], {
		cwd: FRONTEND_ROOT,
		encoding: 'utf8'
	});
	assert.equal(r.status, 0, r.stderr || r.stdout);
});
