// requirement: MIDI-11988
// Unit tests for Hercules DJ Control MP3 e2 map (issue #10840).
// Source: manufacturer User Manual section 6 only; not midi.json or Mixxx.
//
// if registerDeviceMap throws then broken
// if nameMatch claims MP3 LE or Glow then broken
// if a binding source is outside the chart allow-list then broken
// if play/cue/sync/jog/fader are wrong per deck then broken
// if FX effect notes 5-8 or 25-28 are bound then broken
// if hints collide with bindings then broken
// if an LED drives a note this map does not bind then broken
// if HERCULES_MP3_E2_HARDWARE_CONFIRMED is not false then broken
// if the registry omits HERCULES_MP3_E2_MAP then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-control-mp3-e2.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

const CHART_IN = new Set();
for (let n = 1; n <= 46; n++) {
	CHART_IN.add(`1:note:${n}`);
}
for (let c = 48; c <= 60; c++) {
	CHART_IN.add(`1:cc:${c}`);
}

const CHART_LED_OUT = new Set(
	[
		1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 14, 15, 16, 18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 30, 31, 34,
		35, 36, 38, 39, 43, 44, 45, 46
	].map((n) => `1:note:${n}`)
);

function findBindings(pred) {
	return mapModule.HERCULES_MP3_E2_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const DECK_A = {
	play: { ch: 1, kind: 'note', id: 15 },
	cue: { ch: 1, kind: 'note', id: 14 },
	sync: { ch: 1, kind: 'note', id: 18 },
	jog: { ch: 1, kind: 'cc', id: 48 },
	fader: { ch: 1, kind: 'cc', id: 52 }
};

const DECK_B = {
	play: { ch: 1, kind: 'note', id: 35 },
	cue: { ch: 1, kind: 'note', id: 34 },
	sync: { ch: 1, kind: 'note', id: 38 },
	jog: { ch: 1, kind: 'cc', id: 49 },
	fader: { ch: 1, kind: 'cc', id: 57 }
};

const CROSS = { ch: 1, kind: 'cc', id: 56 };

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.HERCULES_MP3_E2_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers MP3 e2 port names only', () => {
	const re = new RegExp(mapModule.HERCULES_MP3_E2_MAP.nameMatch, 'i');
	assert.ok(re.test('DJ Control MP3 e2'));
	assert.ok(re.test('Hercules DJ Control MP3 e2 MIDI'));
	assert.ok(re.test('DJControl MP3 e2'));
	assert.ok(!re.test('DJ Control MP3'));
	assert.ok(!re.test('DJ Control MP3 LE MIDI'));
	assert.ok(!re.test('DJ Control Glow'));
	assert.ok(!re.test('DJControl Inpulse 200'));
});

test('every binding and hint source is in the manufacturer chart', () => {
	const map = mapModule.HERCULES_MP3_E2_MAP;
	for (const b of map.bindings) {
		assert.ok(CHART_IN.has(key(b.source)), `binding not in chart: ${key(b.source)}`);
	}
	for (const h of map.hints) {
		assert.ok(CHART_IN.has(key(h.source)), `hint not in chart: ${key(h.source)}`);
	}
	for (const rule of map.leds) {
		const k = `${rule.out.ch}:note:${rule.out.note}`;
		assert.ok(CHART_LED_OUT.has(k), `LED out not in chart steady list: ${k}`);
	}
});

test('required transport, jog, fader and crossfader per deck', () => {
	for (const [deck, expect] of [
		[1, DECK_A],
		[2, DECK_B]
	]) {
		const on = (type) => theBinding((b) => b.action.type === type && b.action.deck === deck);
		assert.deepEqual(on('deck_play_toggle').source, expect.play);
		assert.deepEqual(on('deck_cue').source, expect.cue);
		assert.deepEqual(on('deck_sync_toggle').source, expect.sync);
		const jog = on('deck_jog');
		assert.deepEqual(jog.source, expect.jog);
		assert.equal(jog.relative, true);
		assert.equal(jog.relativeCentered, undefined);
		const fader = theBinding(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
		);
		assert.deepEqual(fader.source, expect.fader);
	}
	const xf = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(xf.source, CROSS);
});

test('plan table 1a bindings: loops, tempo nudge, EQ invert, no shift rows', () => {
	const map = mapModule.HERCULES_MP3_E2_MAP;
	const eq = map.bindings.filter(
		(b) => b.action.type === 'mixer_channel' && b.action.target === 'eq'
	);
	assert.equal(eq.length, 6);
	for (const b of eq) {
		assert.equal(b.invert, true);
		assert.ok(['high', 'mid', 'low'].includes(b.action.band));
	}
	assert.equal(findBindings((b) => b.shift === true).length, 0, 'no shift:true bindings');
	for (const deck of [1, 2]) {
		const nudges = findBindings(
			(b) => b.action.type === 'deck_tempo_nudge' && b.action.deck === deck
		);
		assert.equal(nudges.length, 2);
		const dirs = new Set(nudges.map((b) => b.action.direction));
		assert.ok(dirs.has(-1) && dirs.has(1));
		const loops = findBindings((b) => b.action.type === 'deck_beat_loop' && b.action.deck === deck);
		assert.equal(loops.length, 4);
		const beats = loops.map((b) => b.action.beats).sort((a, b) => a - b);
		assert.deepEqual(beats, [4, 8, 16, 32]);
	}
});

test('FX criterion: effect notes are hints only (#9788)', () => {
	const fxNotes = new Set([5, 6, 7, 8, 25, 26, 27, 28]);
	const bound = new Set(mapModule.HERCULES_MP3_E2_MAP.bindings.map((b) => key(b.source)));
	for (const n of fxNotes) {
		assert.ok(!bound.has(`1:note:${n}`), `FX note ${n} must not be bound`);
	}
	const hinted = mapModule.HERCULES_MP3_E2_MAP.hints.filter((h) => fxNotes.has(h.source.id));
	assert.equal(hinted.length, 8);
	assert.equal(
		findBindings(
			(b) => b.action.type === 'controller_pad' || b.action.type === 'controller_pad_mode'
		).length,
		0
	);
});

test('hints are unique and do not collide with bindings', () => {
	const map = mapModule.HERCULES_MP3_E2_MAP;
	const bound = new Set(map.bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of map.hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint collides with binding: ${k}`);
		assert.ok(!seen.has(k), `duplicate hint: ${k}`);
		seen.add(k);
	}
});

test('LED rules drive only bound note sources on channel 1', () => {
	const boundNotes = new Set(
		mapModule.HERCULES_MP3_E2_MAP.bindings
			.filter((b) => b.source.kind === 'note')
			.map((b) => key(b.source))
	);
	for (const rule of mapModule.HERCULES_MP3_E2_MAP.leds) {
		assert.equal(rule.out.ch, 1);
		const k = `${rule.out.ch}:note:${rule.out.note}`;
		assert.ok(boundNotes.has(k), `LED on unbound note ${k}`);
	}
});

test('hardware_confirmed is false', () => {
	assert.equal(mapModule.HERCULES_MP3_E2_HARDWARE_CONFIRMED, false);
});

test('the registry carries HERCULES_MP3_E2_MAP', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(mapModule.HERCULES_MP3_E2_MAP));
	assert.equal(
		registry.DEVICE_MAP_REGISTRY.filter(
			(m) => m.nameMatch === mapModule.HERCULES_MP3_E2_MAP.nameMatch
		).length,
		1
	);
});
