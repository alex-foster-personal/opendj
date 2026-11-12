// requirement: MIDI-12023
// Hercules DJ Console Mk2 map (hercules-dj-console-mk2.ts): wires from [MAN] section 7 only.
//
// Regression lines:
//   if a binding source is absent from the 47-row chart allowlist then broken
//   if nameMatch hits 4-Mx or misses MK2 then broken
//   if play/cue/sync/jog/fader/crossfader wires drift from section 7 then broken
//   if jog lacks relative or a button lacks ccButton then broken
//   if FX wires bind to a non-FX action then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

const DECKS = [1, 2];

/** Hand-typed [MAN] section 7 input rows (47); not derived from the map. */
function chartAllowlist() {
	return new Set([
		'1:cc:0x01',
		'1:cc:0x02',
		'1:cc:0x03',
		'1:cc:0x04',
		'1:cc:0x05',
		'1:cc:0x06',
		'1:cc:0x07',
		'1:cc:0x08',
		'1:cc:0x09',
		'1:cc:0x0a',
		'1:cc:0x0b',
		'1:cc:0x0c',
		'1:cc:0x0d',
		'1:cc:0x0e',
		'1:cc:0x0f',
		'1:cc:0x10',
		'1:cc:0x11',
		'1:cc:0x12',
		'1:cc:0x13',
		'1:cc:0x14',
		'1:cc:0x16',
		'1:cc:0x17',
		'1:cc:0x18',
		'1:cc:0x1a',
		'1:cc:0x1b',
		'1:cc:0x1c',
		'1:cc:0x1d',
		'1:cc:0x1e',
		'1:cc:0x21',
		'1:cc:0x22',
		'1:cc:0x23',
		'1:cc:0x24',
		'1:cc:0x28',
		'1:cc:0x2b',
		'1:cc:0x2c',
		'1:cc:0x2d',
		'1:cc:0x2e',
		'1:cc:0x2f',
		'1:cc:0x30',
		'1:cc:0x31',
		'1:cc:0x32',
		'1:cc:0x33',
		'1:cc:0x34',
		'1:cc:0x35',
		'1:cc:0x36',
		'1:cc:0x37',
		'1:cc:0x39'
	]);
}

const FX_WIRES = [0x07, 0x0f, 0x0e, 0x0d, 0x01, 0x10, 0x11, 0x12];
const FX_ACTIONS = new Set(['beat_fx_enable']);

let vite;
let webmidi;
let mapModule;
let registry;

const sourceKey = (s) => `${s.ch}:${s.kind}:0x${s.id.toString(16).padStart(2, '0')}`;

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-console-mk2.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const MAP = () => mapModule.HERCULES_DJ_CONSOLE_MK2_MAP;

function findBindings(pred) {
	return MAP().bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

test('every binding and hint wire is in the hand-typed 47-row chart allowlist', () => {
	const allow = chartAllowlist();
	assert.equal(allow.size, 47);
	for (const b of MAP().bindings) {
		assert.ok(allow.has(sourceKey(b.source)), `binding missing from chart: ${sourceKey(b.source)}`);
	}
	for (const h of MAP().hints) {
		assert.ok(allow.has(sourceKey(h.source)), `hint missing from chart: ${sourceKey(h.source)}`);
	}
});

test('bindings and hints are all ch1 CC and cover the allowlist exactly with no duplicate keys', () => {
	const allow = chartAllowlist();
	const seen = new Set();
	const all = [...MAP().bindings.map((b) => b.source), ...MAP().hints.map((h) => h.source)];
	for (const s of all) {
		assert.equal(s.ch, 1);
		assert.equal(s.kind, 'cc');
		const key = sourceKey(s);
		assert.ok(!seen.has(key), `duplicate wire ${key}`);
		seen.add(key);
	}
	assert.deepEqual(seen, allow);
});

test('nameMatch resolves Mk2 only and registry resolveMapForPort returns this map', () => {
	const re = new RegExp(MAP().nameMatch, 'i');
	assert.ok(re.test('Hercules DJ Console MK2'));
	assert.ok(re.test('Hercules DJ Console Mk2 MIDI'));
	assert.ok(re.test('DJ Console Mk2'));
	assert.ok(!re.test('Hercules DJ Console 4-Mx'));
	assert.ok(!re.test('Hercules DJ Console RMX'));
	assert.ok(!re.test('Hercules DJ Console Mk4'));
	assert.ok(!re.test('Hercules DJ Console Rmx2'));
	assert.ok(!re.test('DJ Console'));
	assert.ok(!re.test('DJ Control Instinct'));
	assert.ok(!re.test('DDJ-FLX4'));

	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	const resolved = webmidi.resolveMapForPort('Hercules DJ Console MK2');
	assert.equal(resolved, MAP());
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('required transport and mixer controls bind the section 7.1 wires per deck', () => {
	const wires = {
		1: {
			play: 0x08,
			cue: 0x09,
			sync: 0x0a,
			jog: 0x36,
			fader: 0x32
		},
		2: {
			play: 0x02,
			cue: 0x03,
			sync: 0x04,
			jog: 0x37,
			fader: 0x33
		}
	};
	for (const deck of DECKS) {
		const w = wires[deck];
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck).source,
			{ ch: 1, kind: 'cc', id: w.play }
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck).source,
			{ ch: 1, kind: 'cc', id: w.cue }
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck).source,
			{ ch: 1, kind: 'cc', id: w.sync }
		);
		assert.deepEqual(
			theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck).source,
			{ ch: 1, kind: 'cc', id: w.jog }
		);
		assert.deepEqual(
			theBinding(
				(b) =>
					b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
			).source,
			{ ch: 1, kind: 'cc', id: w.fader }
		);
	}
	assert.deepEqual(
		theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader').source,
		{ ch: 1, kind: 'cc', id: 0x31 }
	);
});

test('jogs are relative without relativeCentered; buttons have ccButton; faders have neither', () => {
	for (const deck of DECKS) {
		const jog = theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.equal(jog.relative, true);
		assert.notEqual(jog.relativeCentered, true);
	}
	const buttons = findBindings(
		(b) =>
			b.action.type === 'deck_play_toggle' ||
			b.action.type === 'deck_cue' ||
			b.action.type === 'deck_sync_toggle' ||
			b.action.type === 'deck_tempo_nudge' ||
			b.action.type === 'channel_cue'
	);
	for (const b of buttons) {
		assert.equal(b.ccButton, true);
	}
	const analog = findBindings(
		(b) =>
			b.action.type === 'mixer_channel' ||
			(b.action.type === 'mixer_global' && b.action.target === 'crossfader')
	);
	for (const b of analog) {
		assert.notEqual(b.ccButton, true);
		assert.notEqual(b.relative, true);
	}
});

test('FX chart wires are hints or beat_fx_enable, never another action type', () => {
	for (const id of FX_WIRES) {
		const binding = MAP().bindings.find((b) => b.source.id === id);
		const hint = MAP().hints.find((h) => h.source.id === id);
		if (binding) {
			assert.ok(
				FX_ACTIONS.has(binding.action.type),
				`FX wire ${id} bound to ${binding.action.type}`
			);
		} else {
			assert.ok(hint, `FX wire ${id} must be hint or FX binding`);
		}
	}
});

test('no deck_pitch, headphone_mix, headphone_level or browse_encoder bindings', () => {
	const banned = ['deck_pitch', 'headphone_mix', 'headphone_level', 'browse_encoder'];
	const bad = MAP().bindings.filter((b) => banned.includes(b.action.type));
	assert.deepEqual(bad, []);
});

test('EQ low/mid/high CCs match deck-specific section 7 order', () => {
	const eq = {
		1: { low: 0x2e, mid: 0x2f, high: 0x30 },
		2: { low: 0x2b, mid: 0x2c, high: 0x2d }
	};
	for (const deck of DECKS) {
		for (const band of ['low', 'mid', 'high']) {
			const b = theBinding(
				(x) =>
					x.action.type === 'mixer_channel' &&
					x.action.deck === deck &&
					x.action.target === 'eq' &&
					x.action.band === band
			);
			assert.equal(b.source.id, eq[deck][band]);
		}
	}
});

test('DEVICE_MAP_ENTRIES is one live Mk2 key and registerAllDeviceMaps is idempotent', () => {
	assert.equal(mapModule.DEVICE_MAP_ENTRIES.length, 1);
	assert.equal(mapModule.DEVICE_MAP_ENTRIES[0].key, 'HERCULES_DJ_CONSOLE_MK2_MAP');
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

test('leds array is empty because Mk2 LED outputs are CC not notes', () => {
	assert.deepEqual(MAP().leds, []);
});

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(MAP());
	webmidi._resetMidiForTests();
});
