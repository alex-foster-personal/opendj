// requirement: MIDI-11955
// Unit tests for the Kontrol DJ KDJ500 app device map
// (src/lib/rb/midi/maps/kontrol-dj-kdj500.ts) and registry entry.
//
// Wire numbers transcribed from [T3] Konverter template "Traktor Dj v3 (FX)"
// and [MAN] KDJ500 user manual (Kontrol Dj legacy downloads).
//
// Regression lines (single-line format per CLAUDE.md):
//   if validateDeviceMap(KONTROL_DJ_KDJ500_MAP) throws then broken
//   if nameMatch matches Traktor Kontrol S4 MK3, DDJ-FLX4, Mixtour or Mixtrack Pro FX then broken
//   if any binding or hint source is absent from CHART then broken
//   if any CHART wire is neither bound nor hinted then broken
//   if hints collide with bindings or repeat then broken
//   if play, cue, sync, faders, crossfader or jog are on wrong wires then broken
//   if deck_jog lacks relative and invert on CC 0x28/0x2C then broken
//   if FX KNOB 1 is not mixer_channel filter on CC 0x0E/0x12 then broken
//   if a shift_modifier binding or LED rules exist then broken
//   if KONTROL_DJ_KDJ500_MAP is missing from DEVICE_MAP_REGISTRY then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const CH = 1;

/** Every (ch, kind, id) from [T3] on MIDI channel 1. */
const CHART = [
	...[
		0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
		0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f,
		0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2a, 0x2b, 0x2c, 0x2d, 0x2e, 0x2f,
		0x30, 0x31, 0x32, 0x33, 0x36, 0x37, 0x60, 0x61, 0x62, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
		0x6a, 0x6b, 0x6c, 0x6d
	].map((id) => ({ ch: CH, kind: 'note', id })),
	...[
		0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x10,
		0x11, 0x12, 0x13, 0x14, 0x15, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25,
		0x26, 0x27, 0x28, 0x29, 0x2c, 0x2d
	].map((id) => ({ ch: CH, kind: 'cc', id }))
];

const CHART_KEYS = new Set(CHART.map((s) => `${s.ch}:${s.kind}:${s.id}`));

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/kontrol-dj-kdj500.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return mapModule.KONTROL_DJ_KDJ500_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const MAP = () => mapModule.KONTROL_DJ_KDJ500_MAP;

test('validateDeviceMap does not throw', () => {
	webmidi.validateDeviceMap(MAP());
});

test('nameMatch matches Kontrol Dj ports only', () => {
	const re = new RegExp(MAP().nameMatch, 'i');
	assert.ok(re.test('Kontrol Dj Midi In: Port 1'));
	assert.ok(re.test('Kontrol DJ KDJ500'));
	assert.ok(!re.test('Traktor Kontrol S4 MK3'));
	assert.ok(!re.test('DDJ-FLX4'));
	assert.ok(!re.test('Mixtour'));
	assert.ok(!re.test('Mixtrack Pro FX'));
});

test('every binding and hint source is in CHART', () => {
	for (const b of MAP().bindings) {
		assert.ok(CHART_KEYS.has(key(b.source)), `binding missing from CHART: ${key(b.source)}`);
	}
	for (const h of MAP().hints) {
		assert.ok(CHART_KEYS.has(key(h.source)), `hint missing from CHART: ${key(h.source)}`);
	}
});

test('every CHART wire is bound or hinted', () => {
	const covered = new Set([
		...MAP().bindings.map((b) => key(b.source)),
		...MAP().hints.map((h) => key(h.source))
	]);
	for (const k of CHART_KEYS) {
		assert.ok(covered.has(k), `CHART wire ${k} is neither bound nor hinted`);
	}
});

test('hints are unique and never collide with bound sources', () => {
	const bound = new Set(MAP().bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of MAP().hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint collides with binding on ${k}`);
		assert.ok(!seen.has(k), `duplicate hint on ${k}`);
		seen.add(k);
	}
});

test('transport and mixer bindings per deck', () => {
	const expect = {
		1: {
			play: 0x10,
			cue: 0x0e,
			sync: 0x2d,
			pfl: 0x2c,
			load: 0x14,
			loops: [0x09, 0x0a, 0x0b, 0x0c]
		},
		2: { play: 0x26, cue: 0x24, sync: 0x2f, pfl: 0x2e, load: 0x28, loops: [0x1f, 0x20, 0x21, 0x22] }
	};
	for (const deck of [1, 2]) {
		const e = expect[deck];
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck).source,
			{
				ch: CH,
				kind: 'note',
				id: e.play
			}
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck).source,
			{
				ch: CH,
				kind: 'note',
				id: e.cue
			}
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck).source,
			{ ch: CH, kind: 'note', id: e.sync }
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'channel_cue' && b.action.deck === deck).source,
			{
				ch: CH,
				kind: 'note',
				id: e.pfl
			}
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck).source,
			{
				ch: CH,
				kind: 'note',
				id: e.load
			}
		);
		for (let i = 0; i < 4; i++) {
			const beats = [1, 4, 8, 16][i];
			const b = theBinding(
				(x) =>
					x.action.type === 'deck_beat_loop' && x.action.deck === deck && x.action.beats === beats
			);
			assert.deepEqual(b.source, { ch: CH, kind: 'note', id: e.loops[i] });
		}
	}
});

test('mixer strip, pitch, jog and FX on chart CCs', () => {
	const trim = { 1: 0x01, 2: 0x05 };
	const eq = { 1: [0x02, 0x03, 0x04], 2: [0x06, 0x07, 0x08] };
	const fader = { 1: 0x0b, 2: 0x0c };
	const filter = { 1: 0x0e, 2: 0x12 };
	const pitch = { 1: 0x09, 2: 0x0a };
	const jog = { 1: 0x28, 2: 0x2c };
	for (const deck of [1, 2]) {
		assert.deepEqual(
			theBinding(
				(b) =>
					b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'trim'
			).source,
			{ ch: CH, kind: 'cc', id: trim[deck] }
		);
		for (const [band, id] of [
			['high', eq[deck][0]],
			['mid', eq[deck][1]],
			['low', eq[deck][2]]
		]) {
			assert.deepEqual(
				theBinding(
					(b) =>
						b.action.type === 'mixer_channel' &&
						b.action.deck === deck &&
						b.action.target === 'eq' &&
						b.action.band === band
				).source,
				{ ch: CH, kind: 'cc', id }
			);
		}
		assert.deepEqual(
			theBinding(
				(b) =>
					b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
			).source,
			{ ch: CH, kind: 'cc', id: fader[deck] }
		);
		assert.deepEqual(
			theBinding(
				(b) =>
					b.action.type === 'mixer_channel' &&
					b.action.deck === deck &&
					b.action.target === 'filter'
			).source,
			{ ch: CH, kind: 'cc', id: filter[deck] }
		);
		const pitchB = theBinding((b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
		assert.deepEqual(pitchB.source, { ch: CH, kind: 'cc', id: pitch[deck] });
		assert.equal(pitchB.action.lsbOffset, null);
		const jogB = theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.deepEqual(jogB.source, { ch: CH, kind: 'cc', id: jog[deck] });
		assert.equal(jogB.relative, true);
		assert.equal(jogB.invert, true);
	}
	assert.deepEqual(
		theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader').source,
		{ ch: CH, kind: 'cc', id: 0x0d }
	);
});

test('all sources on channel 1, no shift_modifier, no LEDs', () => {
	for (const b of MAP().bindings) assert.equal(b.source.ch, CH);
	for (const h of MAP().hints) assert.equal(h.source.ch, CH);
	assert.equal(findBindings((b) => b.action.type === 'shift_modifier').length, 0);
	assert.equal(MAP().leds, undefined);
});

test('registry carries KONTROL_DJ_KDJ500_MAP live', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(MAP()));
	const entry = mapModule.DEVICE_MAP_ENTRIES.find((e) => e.key === 'KONTROL_DJ_KDJ500_MAP');
	assert.ok(entry);
	assert.equal(entry.live, true);
});
