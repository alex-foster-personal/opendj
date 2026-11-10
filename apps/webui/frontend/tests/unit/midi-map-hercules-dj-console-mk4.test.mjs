// requirement: MIDI-10834
// Hercules DJ Console Mk4 map (hercules-dj-console-mk4.ts): wires from [MAN] section 10 only.
//
// Regression lines:
//   if a binding source is absent from the 59-row chart allowlist then broken
//   if nameMatch hits Mk2 or misses MK4 then broken
//   if play/cue/sync/jog/fader/crossfader wires drift from section 10 then broken
//   if jog lacks relative or a button has ccButton then broken
//   if any binding action type matches /fx/i then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

const DECKS = [1, 2];
const HOT_CUE_SLOTS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

/** Hand-typed [MAN] section 10 input rows (59); not derived from the map. */
function chartAllowlist() {
	return new Set([
		...Array.from({ length: 45 }, (_, i) => `1:note:${i + 1}`),
		'1:cc:48',
		'1:cc:49',
		'1:cc:50',
		'1:cc:51',
		'1:cc:52',
		'1:cc:53',
		'1:cc:54',
		'1:cc:55',
		'1:cc:56',
		'1:cc:57',
		'1:cc:58',
		'1:cc:59',
		'1:cc:60',
		'1:cc:61'
	]);
}

let vite;
let webmidi;
let mapModule;
let registry;

const sourceKey = (s) => `${s.ch}:${s.kind}:${s.id}`;

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-console-mk4.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const MAP = () => mapModule.HERCULES_DJ_CONSOLE_MK4_MAP;

function findBindings(pred) {
	return MAP().bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

test('every binding and hint wire is in the hand-typed 59-row chart allowlist', () => {
	const allow = chartAllowlist();
	assert.equal(allow.size, 59);
	for (const b of MAP().bindings) {
		assert.ok(allow.has(sourceKey(b.source)), `binding missing from chart: ${sourceKey(b.source)}`);
	}
	for (const h of MAP().hints) {
		assert.ok(allow.has(sourceKey(h.source)), `hint missing from chart: ${sourceKey(h.source)}`);
	}
});

test('bindings and hints cover the allowlist exactly with no duplicate keys', () => {
	const allow = chartAllowlist();
	const seen = new Set();
	const all = [...MAP().bindings.map((b) => b.source), ...MAP().hints.map((h) => h.source)];
	for (const s of all) {
		assert.equal(s.ch, 1, 'chart omits channel; map assumes MIDI channel 1');
		const key = sourceKey(s);
		assert.ok(!seen.has(key), `duplicate wire ${key}`);
		seen.add(key);
	}
	assert.deepEqual(seen, allow);
});

test('binding and hint counts and empty leds', () => {
	assert.equal(MAP().bindings.length, 42);
	assert.equal(MAP().hints.length, 17);
	assert.deepEqual(MAP().leds, []);
});

test('required transport and mixer controls bind the section 10 wires per deck', () => {
	const wires = {
		1: { play: 15, cue: 14, sync: 18, jog: 48, fader: 52 },
		2: { play: 35, cue: 34, sync: 38, jog: 49, fader: 57 }
	};
	for (const deck of DECKS) {
		const w = wires[deck];
		const play = theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck);
		assert.deepEqual(play.source, { ch: 1, kind: 'note', id: w.play });
		assert.notEqual(play.ccButton, true);
		const cue = theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.deepEqual(cue.source, { ch: 1, kind: 'note', id: w.cue });
		assert.notEqual(cue.ccButton, true);
		const sync = theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck);
		assert.deepEqual(sync.source, { ch: 1, kind: 'note', id: w.sync });
		assert.notEqual(sync.ccButton, true);
		const jog = theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.deepEqual(jog.source, { ch: 1, kind: 'cc', id: w.jog });
		assert.equal(jog.relative, true);
		assert.notEqual(jog.relativeCentered, true);
		const fader = theBinding(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
		);
		assert.deepEqual(fader.source, { ch: 1, kind: 'cc', id: w.fader });
		assert.notEqual(fader.invert, true);
	}
	const xfader = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(xfader.source, { ch: 1, kind: 'cc', id: 56 });
	assert.notEqual(xfader.invert, true);
});

test('hot cues A-H on chart pad notes per deck', () => {
	const wires = {
		1: [1, 2, 3, 4, 5, 6, 7, 8],
		2: [21, 22, 23, 24, 25, 26, 27, 28]
	};
	for (const deck of DECKS) {
		for (let i = 0; i < 8; i++) {
			const b = theBinding(
				(x) =>
					x.action.type === 'deck_hot_cue' &&
					x.action.deck === deck &&
					x.action.slot === HOT_CUE_SLOTS[i]
			);
			assert.deepEqual(b.source, { ch: 1, kind: 'note', id: wires[deck][i] });
		}
	}
});

test('EQ bands and headphone_mix use invert on chart analog CCs', () => {
	const eq = {
		1: { high: 53, mid: 54, low: 55 },
		2: { high: 58, mid: 59, low: 60 }
	};
	for (const deck of DECKS) {
		for (const band of ['high', 'mid', 'low']) {
			const b = theBinding(
				(x) =>
					x.action.type === 'mixer_channel' &&
					x.action.deck === deck &&
					x.action.target === 'eq' &&
					x.action.band === band
			);
			assert.equal(b.source.id, eq[deck][band]);
			assert.equal(b.invert, true);
		}
	}
	const mix = theBinding((b) => b.action.type === 'headphone_mix');
	assert.equal(mix.source.id, 61);
	assert.equal(mix.invert, true);
});

test('no FX action bindings; Source and pitch are hints only', () => {
	const fxBindings = MAP().bindings.filter((b) => /fx/i.test(b.action.type));
	assert.deepEqual(fxBindings, []);
	assert.ok(MAP().hints.some((h) => h.source.kind === 'note' && h.source.id === 19));
	assert.ok(MAP().hints.some((h) => h.source.kind === 'note' && h.source.id === 39));
	assert.ok(MAP().hints.some((h) => h.source.kind === 'cc' && h.source.id === 50));
	assert.ok(MAP().hints.some((h) => h.source.kind === 'cc' && h.source.id === 51));
	assert.equal(
		MAP().bindings.find((b) => b.source.id === 19 || b.source.id === 39),
		undefined
	);
	assert.equal(
		MAP().bindings.find(
			(b) => b.source.kind === 'cc' && (b.source.id === 50 || b.source.id === 51)
		),
		undefined
	);
});

test('nameMatch resolves Mk4 only and registry resolveMapForPort returns this map', () => {
	const re = new RegExp(MAP().nameMatch, 'i');
	assert.ok(re.test('Hercules DJ Console MK4'));
	assert.ok(re.test('DJ Console Mk4'));
	assert.ok(!re.test('Hercules DJ Console 4-Mx'));
	assert.ok(!re.test('Hercules DJ Console RMX'));
	assert.ok(!re.test('Hercules DJ Console Rmx2'));
	assert.ok(!re.test('Hercules DJ Console Mk2'));
	assert.ok(!re.test('DDJ-FLX4'));

	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	const resolved = webmidi.resolveMapForPort('Hercules DJ Console MK4');
	assert.equal(resolved, MAP());
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('DEVICE_MAP_ENTRIES is one live Mk4 key and registerAllDeviceMaps is idempotent', () => {
	assert.equal(mapModule.DEVICE_MAP_ENTRIES.length, 1);
	assert.equal(mapModule.DEVICE_MAP_ENTRIES[0].key, 'HERCULES_DJ_CONSOLE_MK4_MAP');
	assert.equal(mapModule.DEVICE_MAP_ENTRIES[0].live, true);
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	const count = registry.DEVICE_MAP_REGISTRY.filter((m) => m.nameMatch === MAP().nameMatch).length;
	assert.equal(count, 1);
	registry.registerAllDeviceMaps();
	assert.equal(
		registry.DEVICE_MAP_REGISTRY.filter((m) => m.nameMatch === MAP().nameMatch).length,
		1
	);
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(MAP());
	webmidi._resetMidiForTests();
});
