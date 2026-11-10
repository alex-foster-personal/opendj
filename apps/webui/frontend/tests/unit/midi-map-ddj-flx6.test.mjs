// requirement: MIDI-11948
// Unit tests for the AlphaTheta DDJ-FLX6 app map (src/lib/rb/midi/maps/ddj-flx6.ts).
// Same vite+svelte bootstrap as flx10-map.test.mjs.
//
// CHART is a literal transcription of the official AlphaTheta "DDJ-FLX6 List of
// MIDI messages" PDF (E1) rows the map may bind, as (ch, kind, id) triples.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(FLX6_MAP) throws then broken (dupes/invalid)
//   if nameMatch doesn't match 'DDJ-FLX6' / 'DDJ-FLX6-GT' or matches other FLX models then broken
//   if any binding's (ch, kind, id) is not a chart row then broken
//   if play/cue/sync/fader/jog per deck or the crossfader are unbound then broken
//   if each deck's FILTER (colour FX) isn't bound to mixer_channel filter then broken
//   if a source is both bound and hinted then broken
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

// [PDF] rows, copied from the chart: 1-1..1-17, 3-5..3-9 (decks, ch 1-4),
// 5-5 pads (ch 8/10/12/14), 3-2/3-3/3-7/3-10/3-12/3-13/4-1/4-4..4-7 (ch 7).
const CHART = new Set([
	...DECKS.flatMap((ch) => [
		...[0x0b, 0x0c, 0x3f, 0x14, 0x4d, 0x58, 0x54].map((id) => `${ch}:note:${id}`),
		...[0x00, 0x04, 0x07, 0x0b, 0x0f, 0x13, 0x21, 0x22, 0x23].map((id) => `${ch}:cc:${id}`)
	]),
	...[8, 10, 12, 14].flatMap((ch) =>
		[...range(0x00, 0x07), ...range(0x60, 0x67)].map((id) => `${ch}:note:${id}`)
	),
	...[0x17, 0x18, 0x19, 0x1a, 0x40, 0x64, 0x1f, 0x08, 0x0c, 0x0d].map((id) => `7:cc:${id}`),
	...[0x46, 0x47, 0x48, 0x49, 0x63].map((id) => `7:note:${id}`)
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
	map = (await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx6.ts')).FLX6_MAP;
});

after(async () => {
	await vite.close();
});

test('FLX6 map registers without throwing', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(map);
	webmidi._resetMidiForTests();
});

test('nameMatch matches the FLX6 USB names only', () => {
	const re = new RegExp(map.nameMatch, 'i');
	assert.ok(re.test('DDJ-FLX6'));
	assert.ok(re.test('DDJ-FLX6-GT'));
	assert.ok(!re.test('DDJ-FLX4'));
	assert.ok(!re.test('DDJ-FLX10'));
});

test('every binding is a manufacturer chart row', () => {
	const offChart = map.bindings.map((b) => key(b.source)).filter((k) => !CHART.has(k));
	assert.deepEqual(offChart, []);
});

test('play, cue, sync, fader and jog are bound per deck; crossfader bound', () => {
	for (const deck of DECKS) {
		const has = (kind, id, type) =>
			bindingsAt(deck, kind, id).some((b) => b.action.type === type && b.action.deck === deck);
		assert.ok(has('note', 0x0b, 'deck_play_toggle'), `play ${deck}`);
		assert.ok(has('note', 0x0c, 'deck_cue'), `cue ${deck}`);
		assert.ok(has('note', 0x58, 'deck_sync_toggle'), `sync ${deck}`);
		assert.ok(
			bindingsAt(deck, 'cc', 0x13).some((b) => b.action.target === 'fader'),
			`fader ${deck}`
		);
		for (const cc of [0x22, 0x23, 0x21]) {
			const jog = bindingsAt(deck, 'cc', cc).find((b) => b.action.type === 'deck_jog');
			assert.ok(jog && jog.action.deck === deck, `jog ${deck} cc ${cc}`);
			assert.equal(jog.relative, true);
			assert.equal(jog.relativeCentered, true);
		}
	}
	assert.ok(bindingsAt(7, 'cc', 0x1f).some((b) => b.action.target === 'crossfader'));
});

test('FILTER colour FX bound per deck; Beat FX stays a hint until a beat-FX action exists', () => {
	for (const deck of DECKS) {
		const filter = bindingsAt(7, 'cc', 0x16 + deck);
		assert.ok(
			filter.some((b) => b.action.type === 'mixer_channel' && b.action.target === 'filter')
		);
	}
	for (const ch of [5, 6]) {
		for (const [kind, id] of [
			['note', 0x47],
			['cc', 0x02]
		]) {
			assert.equal(bindingsAt(ch, kind, id).length, 0);
			assert.ok(map.hints.some((h) => key(h.source) === `${ch}:${kind}:${id}`));
		}
	}
});

test('no source is both bound and hinted', () => {
	const bound = new Set(map.bindings.map((b) => key(b.source)));
	assert.deepEqual(
		map.hints.map((h) => key(h.source)).filter((k) => bound.has(k)),
		[]
	);
});

test('device-maps.json matches the export script output', () => {
	const run = spawnSync(process.execPath, ['scripts/export-midi-maps.mjs', '--check'], {
		cwd: FRONTEND_ROOT
	});
	assert.equal(run.status, 0, String(run.stderr));
});
