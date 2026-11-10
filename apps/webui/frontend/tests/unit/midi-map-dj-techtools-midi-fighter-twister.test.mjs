// requirement: MIDI-11993
// Unit tests for the DJ TechTools Midi Fighter Twister device map
// (src/lib/rb/midi/maps/dj-techtools-midi-fighter-twister.ts) and registry entry.
//
// Every expected wire number is read off the manufacturer User Guide Ver 2026.06.01
// Appendix 1 "Bank 1 MIDI" (p.17).
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(MIDI_FIGHTER_TWISTER_MAP) throws then broken
//   if any binding (ch, kind, id) is outside the guide Appendix 1 chart then broken
//   if deck 1 EQ high is not ch 1 CC 0 or any binding uses ch 0 or ch 3 then broken
//   if play, cue, sync, both faders, crossfader, jogs or browse are not bound then broken
//   if jog or browse encoders are not relative-centred or mixer encoders are relative then broken
//   if colour FX filter is missing per deck or any action type contains fx then broken
//   if ch 2 bindings lack ccButton then broken
//   if hints collide with bindings or repeat then broken
//   if nameMatch matches Spectra, 3D or Pioneer hardware then broken
//   if MIDI_FIGHTER_TWISTER_MAP is missing from DEVICE_MAP_REGISTRY then broken

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
let decode;

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function buildChartKeys() {
	const keys = new Set();
	for (let cc = 0; cc <= 63; cc++) {
		keys.add(`1:cc:${cc}`);
		keys.add(`2:cc:${cc}`);
	}
	for (let cc = 8; cc <= 31; cc++) keys.add(`4:cc:${cc}`);
	return keys;
}

const CHART = buildChartKeys();

function isBank1Binding(b) {
	const { ch, kind, id } = b.source;
	if (kind !== 'cc') return false;
	if (ch === 1 || ch === 2) return id >= 0 && id <= 15;
	if (ch === 4) return id >= 8 && id <= 13;
	return false;
}

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
	mapModule = await vite.ssrLoadModule(
		'/src/lib/rb/midi/maps/dj-techtools-midi-fighter-twister.ts'
	);
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
	decode = await vite.ssrLoadModule('/src/lib/rb/midi/decode.ts');
});

after(async () => {
	await vite.close();
});

const MAP = () => mapModule.MIDI_FIGHTER_TWISTER_MAP;

function findBindings(pred) {
	return MAP().bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(MAP());
	webmidi._resetMidiForTests();
});

test('every binding and hint source is in the guide Appendix 1 chart and bank 1 only', () => {
	for (const b of MAP().bindings) {
		assert.ok(CHART.has(key(b.source)), `binding off chart: ${key(b.source)}`);
		assert.ok(isBank1Binding(b), `not bank 1: ${key(b.source)}`);
	}
	for (const h of MAP().hints) {
		assert.ok(CHART.has(key(h.source)), `hint off chart: ${key(h.source)}`);
	}
});

test('deck 1 EQ high is ch 1 CC 0 and no binding uses ch 0 or ch 3', () => {
	const eqHigh = theBinding(
		(b) =>
			b.action.type === 'mixer_channel' &&
			b.action.deck === 1 &&
			b.action.target === 'eq' &&
			b.action.band === 'high'
	);
	assert.deepEqual(eqHigh.source, { ch: 1, kind: 'cc', id: 0 });
	for (const b of MAP().bindings) {
		assert.notEqual(b.source.ch, 0, 'ch 0 forbidden');
		assert.notEqual(b.source.ch, 3, 'ch 3 forbidden');
	}
});

test('bank 1 encoder and switch tables match the plan (26 bindings)', () => {
	assert.equal(MAP().bindings.length, 26);
	const encCases = [
		[0, { type: 'mixer_channel', deck: 1, target: 'eq', band: 'high' }, {}],
		[1, { type: 'mixer_channel', deck: 1, target: 'trim' }, {}],
		[2, { type: 'mixer_channel', deck: 2, target: 'trim' }, {}],
		[3, { type: 'mixer_channel', deck: 2, target: 'eq', band: 'high' }, {}],
		[4, { type: 'mixer_channel', deck: 1, target: 'eq', band: 'mid' }, {}],
		[5, { type: 'mixer_channel', deck: 1, target: 'filter' }, {}],
		[6, { type: 'mixer_channel', deck: 2, target: 'filter' }, {}],
		[7, { type: 'mixer_channel', deck: 2, target: 'eq', band: 'mid' }, {}],
		[8, { type: 'mixer_channel', deck: 1, target: 'eq', band: 'low' }, {}],
		[9, { type: 'mixer_global', target: 'crossfader' }, {}],
		[
			10,
			{ type: 'browse_encoder' },
			{ relative: true, relativeCentered: true, relativeUnit: true }
		],
		[11, { type: 'mixer_channel', deck: 2, target: 'eq', band: 'low' }, {}],
		[12, { type: 'mixer_channel', deck: 1, target: 'fader' }, {}],
		[13, { type: 'deck_jog', deck: 1 }, { relative: true, relativeCentered: true }],
		[14, { type: 'deck_jog', deck: 2 }, { relative: true, relativeCentered: true }],
		[15, { type: 'mixer_channel', deck: 2, target: 'fader' }, {}]
	];
	for (const [cc, action, flags] of encCases) {
		const b = theBinding((x) => x.source.ch === 1 && x.source.id === cc);
		assert.deepEqual(b.action, action);
		for (const [k, v] of Object.entries(flags)) assert.equal(b[k], v, `enc ${cc} ${k}`);
		if (Object.keys(flags).length === 0) {
			assert.equal(b.relative, undefined, `enc ${cc} must not be relative`);
		}
	}
	const swCases = [
		[1, { type: 'deck_sync_toggle', deck: 1 }],
		[2, { type: 'deck_sync_toggle', deck: 2 }],
		[5, { type: 'deck_cue', deck: 1 }],
		[6, { type: 'deck_cue', deck: 2 }],
		[9, { type: 'browse_load', deck: 1 }],
		[10, { type: 'browse_load', deck: 2 }],
		[12, { type: 'channel_cue', deck: 1 }],
		[15, { type: 'channel_cue', deck: 2 }],
		[13, { type: 'deck_play_toggle', deck: 1 }],
		[14, { type: 'deck_play_toggle', deck: 2 }]
	];
	for (const [cc, action] of swCases) {
		const b = theBinding((x) => x.source.ch === 2 && x.source.id === cc);
		assert.deepEqual(b.action, action);
		assert.equal(b.ccButton, true);
	}
});

test('required transport, mixer and browse coverage per deck', () => {
	for (const deck of [1, 2]) {
		assert.equal(
			findBindings((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck).length,
			1
		);
		assert.equal(
			findBindings((b) => b.action.type === 'deck_cue' && b.action.deck === deck).length,
			1
		);
		assert.equal(
			findBindings((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck).length,
			1
		);
		assert.equal(
			findBindings((b) => b.action.type === 'deck_jog' && b.action.deck === deck).length,
			1
		);
		assert.equal(
			findBindings(
				(b) =>
					b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
			).length,
			1
		);
	}
	assert.equal(findBindings((b) => b.action.type === 'mixer_global').length, 1);
	assert.equal(findBindings((b) => b.action.type === 'browse_encoder').length, 1);
});

test('relative flags match encoder MIDI type requirements', () => {
	for (const b of MAP().bindings) {
		if (b.action.type === 'deck_jog' || b.action.type === 'browse_encoder') {
			assert.equal(b.relative, true);
			assert.equal(b.relativeCentered, true);
		}
		if (b.action.type === 'mixer_channel' || b.action.type === 'mixer_global') {
			assert.equal(b.relative, undefined);
			assert.equal(b.relativeCentered, undefined);
		}
		if (b.source.ch === 2) assert.equal(b.ccButton, true);
	}
});

test('Enc 3FH/41H decode matches decodeRelativeCentered', () => {
	assert.equal(decode.decodeRelativeCentered(65), 1);
	assert.equal(decode.decodeRelativeCentered(63), -1);
});

test('each deck has one filter (colour FX) and no fx action types', () => {
	for (const deck of [1, 2]) {
		assert.equal(
			findBindings(
				(b) =>
					b.action.type === 'mixer_channel' &&
					b.action.deck === deck &&
					b.action.target === 'filter'
			).length,
			1
		);
	}
	for (const b of MAP().bindings) {
		assert.ok(!String(b.action.type).includes('fx'), b.action.type);
	}
});

test('nameMatch and resolveMapForPort', () => {
	const re = new RegExp(MAP().nameMatch, 'i');
	assert.ok(re.test('Midi Fighter Twister'));
	assert.ok(re.test('MIDI Fighter Twister'));
	assert.ok(re.test('Midi Fighter Twister 2'));
	assert.ok(!re.test('Midi Fighter 3D'));
	assert.ok(!re.test('Midi Fighter Spectra'));
	assert.ok(!re.test('DDJ-FLX4'));
	assert.ok(!re.test('DJM-A9'));
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	assert.equal(webmidi.resolveMapForPort('Midi Fighter Twister'), MAP());
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('twelve hints do not collide with bindings', () => {
	assert.equal(MAP().hints.length, 12);
	const bound = new Set(MAP().bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of MAP().hints) {
		assert.ok(!bound.has(key(h.source)), `hint collides: ${key(h.source)}`);
		assert.ok(!seen.has(key(h.source)), `hint repeats: ${key(h.source)}`);
		seen.add(key(h.source));
		assert.ok(h.label.length > 0);
	}
});

test('no LED rules on map', () => {
	assert.equal(MAP().leds, undefined);
});

test('DEVICE_MAP_REGISTRY contains MIDI_FIGHTER_TWISTER_MAP', async () => {
	const entries = await vite.ssrLoadModule('/src/lib/rb/midi/maps/entries.ts');
	const hit = entries.DEVICE_MAP_ENTRIES.find((e) => e.key === 'MIDI_FIGHTER_TWISTER_MAP');
	assert.ok(hit);
	assert.equal(hit.map, MAP());
	assert.equal(hit.live, true);
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(MAP()));
});
