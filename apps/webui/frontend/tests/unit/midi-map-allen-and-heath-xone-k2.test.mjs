// Unit tests for the Allen & Heath XONE:K2 device map
// (src/lib/rb/midi/maps/allen-and-heath-xone-k2.ts). Same vite+svelte bootstrap
// as mixtour-map.test.mjs.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(XONE_K2_MAP) throws then broken (dupes/invalid)
//   if nameMatch rejects XONE:K2 from AP8509 p.6 then broken
//   if a binding source is outside the AP8509 layers-off Send chart then broken
//   if play/cue/sync/jog/fader/crossfader/filter wires drift then broken
//   if bindings+hints do not cover chart exactly once (58 wires) then broken
//   if DEVICE_MAP_ENTRIES is not [{ key: XONE_K2_MAP, live: true }] then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/allen-and-heath-xone-k2.ts');
});

after(async () => {
	await vite.close();
});

const CH = 15;

// [AP8509 pp.14-16] layers-off Send wires on default channel 15
const CHART_CC = [...Array(22).keys()]; // CC 0..21
const CHART_NOTES = [12, 13, 14, 15, ...Array.from({ length: 32 }, (_, i) => 24 + i)]; // 12..15, 24..55

function chartKeys() {
	const keys = new Set();
	for (const id of CHART_CC) keys.add(`${CH}:cc:${id}`);
	for (const id of CHART_NOTES) keys.add(`${CH}:note:${id}`);
	return keys;
}

const wireKey = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return mapModule.XONE_K2_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.XONE_K2_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers XONE:K2 port names and rejects siblings', () => {
	const re = new RegExp(mapModule.XONE_K2_MAP.nameMatch, 'i');
	assert.ok(re.test('XONE:K2'));
	assert.ok(re.test('XONE:K2 MIDI 1'));
	assert.ok(re.test('Allen and Heath Xone K2'));
	assert.ok(re.test('Xone K2'));
	assert.ok(!re.test('XONE:K1'));
	assert.ok(!re.test('XONE:K3'));
	assert.ok(!re.test('XONE:DB4'));
	assert.ok(!re.test('XONE:96'));
	assert.ok(!re.test('DDJ-FLX4'));
	assert.ok(!re.test('Reloop Mixtour'));
});

test('every binding and hint source is on ch 15 and in the AP8509 chart', () => {
	const chart = chartKeys();
	const { bindings, hints } = mapModule.XONE_K2_MAP;
	for (const b of bindings) {
		assert.equal(b.source.ch, CH);
		assert.ok(chart.has(wireKey(b.source)), `binding ${wireKey(b.source)} not in chart`);
	}
	for (const h of hints) {
		assert.equal(h.source.ch, CH);
		assert.ok(chart.has(wireKey(h.source)), `hint ${wireKey(h.source)} not in chart`);
	}
});

test('bindings and hints cover the chart exactly once (37 bindings, 21 hints)', () => {
	const seen = new Set();
	const collect = (source) => {
		const k = wireKey(source);
		assert.ok(!seen.has(k), `duplicate wire ${k}`);
		seen.add(k);
	};
	for (const b of mapModule.XONE_K2_MAP.bindings) collect(b.source);
	for (const h of mapModule.XONE_K2_MAP.hints) collect(h.source);
	assert.equal(mapModule.XONE_K2_MAP.bindings.length, 37);
	assert.equal(mapModule.XONE_K2_MAP.hints.length, 21);
	assert.equal(seen.size, 58);
	assert.deepEqual(seen, chartKeys());
});

test('acceptance transport, jog, faders, crossfader, browse per deck', () => {
	for (const [deck, play, cue, sync, faderCc, jogCc, filterCc] of [
		[1, 25, 24, 29, 17, 0, 8],
		[2, 27, 26, 31, 18, 3, 11]
	]) {
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck).source,
			{ ch: CH, kind: 'note', id: play }
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck).source,
			{ ch: CH, kind: 'note', id: cue }
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck).source,
			{ ch: CH, kind: 'note', id: sync }
		);
		assert.deepEqual(
			theBinding(
				(b) =>
					b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
			).source,
			{ ch: CH, kind: 'cc', id: faderCc }
		);
		const jog = theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.deepEqual(jog.source, { ch: CH, kind: 'cc', id: jogCc });
		assert.equal(jog.relative, true);
		assert.deepEqual(
			theBinding(
				(b) =>
					b.action.type === 'mixer_channel' &&
					b.action.deck === deck &&
					b.action.target === 'filter'
			).source,
			{ ch: CH, kind: 'cc', id: filterCc }
		);
	}
	const xf = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(xf.source, { ch: CH, kind: 'cc', id: 12 });
	const master = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'master'
	);
	assert.deepEqual(master.source, { ch: CH, kind: 'cc', id: 15 });
	const browse = theBinding((b) => b.action.type === 'browse_encoder');
	assert.deepEqual(browse.source, { ch: CH, kind: 'cc', id: 21 });
	assert.equal(browse.relative, true);
});

test('EQ, trim, PFL, hot cues, load, pitch per deck', () => {
	for (const [deck, trim, eqHi, eqMid, eqLow, pfl, load, pitchCc] of [
		[1, 4, 5, 9, 13, 41, 28, 16],
		[2, 7, 6, 10, 14, 42, 30, 19]
	]) {
		assert.deepEqual(
			theBinding(
				(b) =>
					b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'trim'
			).source,
			{ ch: CH, kind: 'cc', id: trim }
		);
		for (const [band, cc] of [
			['high', eqHi],
			['mid', eqMid],
			['low', eqLow]
		]) {
			assert.deepEqual(
				theBinding(
					(b) =>
						b.action.type === 'mixer_channel' &&
						b.action.deck === deck &&
						b.action.target === 'eq' &&
						b.action.band === band
				).source,
				{ ch: CH, kind: 'cc', id: cc }
			);
		}
		assert.deepEqual(
			theBinding((b) => b.action.type === 'channel_cue' && b.action.deck === deck).source,
			{ ch: CH, kind: 'note', id: pfl }
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck).source,
			{ ch: CH, kind: 'note', id: load }
		);
		const pitch = theBinding((b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
		assert.deepEqual(pitch.source, { ch: CH, kind: 'cc', id: pitchCc });
		assert.equal(pitch.action.lsbOffset, null);
		assert.equal(pitch.invert, true);
	}
	const slots = [
		[1, 'A', 36],
		[1, 'B', 37],
		[1, 'C', 32],
		[1, 'D', 33],
		[2, 'A', 38],
		[2, 'B', 39],
		[2, 'C', 34],
		[2, 'D', 35]
	];
	for (const [deck, slot, note] of slots) {
		assert.deepEqual(
			theBinding(
				(b) => b.action.type === 'deck_hot_cue' && b.action.deck === deck && b.action.slot === slot
			).source,
			{ ch: CH, kind: 'note', id: note }
		);
	}
});

test('no pad mode, shift bindings, leds, meters, init or nativeAudioProfile', () => {
	const { bindings } = mapModule.XONE_K2_MAP;
	const map = mapModule.XONE_K2_MAP;
	assert.deepEqual(
		bindings.filter(
			(b) => b.action.type === 'controller_pad_mode' || b.action.type === 'controller_pad'
		),
		[]
	);
	assert.deepEqual(
		bindings.filter((b) => b.shift),
		[]
	);
	assert.equal(map.leds, undefined);
	assert.equal(map.meters, undefined);
	assert.equal(map.init, undefined);
	assert.equal(map.nativeAudioProfile, undefined);
});

test('DEVICE_MAP_ENTRIES self-registers XONE_K2_MAP as live', () => {
	assert.deepEqual(mapModule.DEVICE_MAP_ENTRIES, [
		{ key: 'XONE_K2_MAP', map: mapModule.XONE_K2_MAP, live: true }
	]);
});
