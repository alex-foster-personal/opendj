// requirement: MIDI-12039
// Unit tests for the Pioneer DJ CDJ-850 app map (src/lib/rb/midi/maps/cdj-850.ts).
// Same vite+svelte bootstrap as midi-map-ddj-flx6.test.mjs.
//
// CHART is a literal transcription of the CDJ-850 "List of MIDI Messages"
// (printed p26 to p27), as (ch, kind, id) triples on channels 1 to 4.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(CDJ850_MAP) throws then broken (dupes/invalid)
//   if nameMatch doesn't match CDJ-850 variants or matches other CDJ models then broken
//   if any binding's (ch, kind, id) is not a chart row then broken
//   if any hint's (ch, kind, id) is not a chart row then broken
//   if a chart row is neither bound nor hinted then broken
//   if a source is both bound and hinted then broken
//   if play/cue/jog/tempo/loops/browse per deck are unbound then broken
//   if deck_sync_toggle/mixer_channel/mixer_global bindings exist then broken (no chart row)
//   if map.leds is non-empty then broken (chart is MIDI-IN only)
//   if device-maps.json differs from the export script output then broken

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const DECKS = [1, 2, 3, 4];
const range = (lo, hi) => Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);

const NOTE_IDS = [
	...range(0x00, 0x08),
	...range(0x0a, 0x0e),
	0x10,
	0x11,
	0x12,
	0x20,
	0x21,
	0x28,
	0x29,
	0x2a,
	0x2b,
	0x2d,
	0x2f,
	0x30,
	0x32,
	0x33,
	0x36,
	0x37,
	0x38,
	0x3f
];
const CC_IDS = [0x10, 0x1d, 0x1e, 0x30, 0x4f];

const CHART = new Set([
	...DECKS.flatMap((ch) => [
		...CC_IDS.map((id) => `${ch}:cc:${id}`),
		...NOTE_IDS.map((id) => `${ch}:note:${id}`)
	])
]);

let vite;
let webmidi;
let map;

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
	map = (await vite.ssrLoadModule('/src/lib/rb/midi/maps/cdj-850.ts')).CDJ850_MAP;
});

after(async () => {
	await vite.close();
});

test('CDJ-850 map registers without throwing', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(map);
	webmidi._resetMidiForTests();
});

test('nameMatch matches the CDJ-850 USB names only', () => {
	const re = new RegExp(map.nameMatch, 'i');
	assert.ok(re.test('CDJ-850'));
	assert.ok(re.test('CDJ-850-K'));
	assert.ok(re.test('Pioneer DJ CDJ-850'));
	assert.ok(!re.test('CDJ-3000'));
	assert.ok(!re.test('CDJ-900'));
	assert.ok(!re.test('DDJ-FLX6'));
});

test('every binding is a manufacturer chart row', () => {
	const offChart = map.bindings.map((b) => key(b.source)).filter((k) => !CHART.has(k));
	assert.deepEqual(offChart, []);
});

test('every hint is a manufacturer chart row', () => {
	const offChart = map.hints.map((h) => key(h.source)).filter((k) => !CHART.has(k));
	assert.deepEqual(offChart, []);
});

test('every chart row is bound or hinted exactly once; bound and hinted are disjoint', () => {
	const bound = new Set(map.bindings.map((b) => key(b.source)));
	const hinted = new Set(map.hints.map((h) => key(h.source)));
	const overlap = [...bound].filter((k) => hinted.has(k));
	assert.deepEqual(overlap, []);
	const covered = new Set([...bound, ...hinted]);
	const missing = [...CHART].filter((k) => !covered.has(k));
	assert.deepEqual(missing, []);
	const extra = [...covered].filter((k) => !CHART.has(k));
	assert.deepEqual(extra, []);
	const hintDupes = map.hints.map((h) => key(h.source));
	assert.equal(new Set(hintDupes).size, hintDupes.length);
});

test('play, cue, jog, tempo, loops, browse and beat loops bound per deck', () => {
	for (const deck of DECKS) {
		const has = (kind, id, type, extra = () => true) =>
			bindingsAt(deck, kind, id).some(
				(b) =>
					b.action.type === type &&
					(b.action.deck === undefined || b.action.deck === deck) &&
					extra(b)
			);
		assert.ok(has('note', 0x00, 'deck_play_toggle'), `play deck ${deck}`);
		assert.ok(has('note', 0x01, 'deck_cue'), `cue deck ${deck}`);
		assert.ok(
			has('cc', 0x1d, 'deck_pitch', (b) => b.action.lsbOffset === null),
			`tempo deck ${deck}`
		);
		assert.ok(has('note', 0x06, 'deck_manual_loop_cycle'), `loop in deck ${deck}`);
		assert.ok(has('note', 0x07, 'deck_manual_loop_cycle'), `loop out deck ${deck}`);
		assert.ok(has('note', 0x08, 'deck_loop_exit'), `reloop deck ${deck}`);
		assert.ok(has('note', 0x33, 'browse_load'), `browse load deck ${deck}`);
		const enc = bindingsAt(deck, 'cc', 0x4f).find((b) => b.action.type === 'browse_encoder');
		assert.ok(enc, `browse encoder deck ${deck}`);
		assert.equal(enc.relative, true);
		assert.equal(enc.relativeCentered, undefined);
		for (const cc of [0x10, 0x30]) {
			const jog = bindingsAt(deck, 'cc', cc).find((b) => b.action.type === 'deck_jog');
			assert.ok(jog && jog.action.deck === deck, `jog deck ${deck} cc 0x${cc.toString(16)}`);
			assert.equal(jog.relative, true);
			assert.equal(jog.relativeCentered, true);
		}
		const loops = [
			[0x28, 1],
			[0x29, 2],
			[0x2a, 4],
			[0x2b, 8]
		];
		for (const [noteId, beats] of loops) {
			const bl = bindingsAt(deck, 'note', noteId).find((b) => b.action.type === 'deck_beat_loop');
			assert.ok(bl && bl.action.beats === beats, `beat loop ${beats} deck ${deck}`);
		}
	}
});

test('no sync, fader, crossfader or mixer bindings (chart has no such rows)', () => {
	const forbidden = ['deck_sync_toggle', 'mixer_channel', 'mixer_global'];
	const found = map.bindings.filter((b) => forbidden.includes(b.action.type));
	assert.deepEqual(found, []);
});

test('map has no LED rules (MIDI-IN only chart)', () => {
	assert.deepEqual(map.leds, []);
});

test('every hint label is a non-empty string', () => {
	for (const h of map.hints) {
		assert.equal(typeof h.label, 'string');
		assert.ok(h.label.length > 0);
	}
});

test('device-maps.json matches the export script output', () => {
	const run = spawnSync(process.execPath, ['scripts/export-midi-maps.mjs', '--check'], {
		cwd: FRONTEND_ROOT
	});
	assert.equal(run.status, 0, String(run.stderr));
});
