// requirement: MIDI-12054
// Unit tests for the DJ-Tech MIX-101 live device map
// (src/lib/rb/midi/maps/dj-tech-mix-101.ts) + its entry in the maps registry.
//
// Every expected wire number is read off the DJ-Tech MIX-101 User Manual MIDI
// tables (printed pp. 28-30). Nothing here is sourced from
// tools/deck-diagrams/devices/dj-tech-mix-101/midi.json or Mixxx.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(DJ_TECH_MIX_101_MAP) throws then broken
//   if nameMatch misses DJ-Tech Mix-101 / MIX-101 or matches DJM-101 then broken
//   if any binding or hint source is outside the manufacturer chart then broken
//   if play/cue/sync/load/jog per deck or faders/crossfader are unbound then broken
//   if A/B is not the only shift_modifier or shift:true bindings are not deck 2 then broken
//   if MODE FX / keys 1-2 are not controller_pad mode/pads (#9788) then broken
//   if cc 0x26 is duplicated or CF CURVE binds cc 0x23 then broken
//   if binding keys repeat, or hints collide with unshifted bindings then broken
//   if the registry does not carry DJ_TECH_MIX_101_MAP then broken
//   if device-maps.json differs from export script output then broken

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const DECKS = [1, 2];

const CHART_NOTES = [
	0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0b, 0x0c, 0x0d, 0x11, 0x12, 0x13, 0x14,
	0x15, 0x50, 0x51, 0x52, 0x53, 0x54, 0x55
];
const CHART_CCS = [
	0x0e, 0x10, 0x11, 0x12, 0x13, 0x15, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2a,
	0x2b
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
const bindingKey = (b) => `${b.shift === true ? 'S' : '-'}:${key(b.source)}`;

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/dj-tech-mix-101.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const MAP = () => mapModule.DJ_TECH_MIX_101_MAP;

function findBindings(pred) {
	return MAP().bindings.filter(pred);
}

function bindingsAt(ch, kind, id) {
	return MAP().bindings.filter(
		(b) => b.source.ch === ch && b.source.kind === kind && b.source.id === id
	);
}

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(MAP());
	webmidi._resetMidiForTests();
});

test('nameMatch covers MIX-101 port names and rejects lookalikes', () => {
	const re = new RegExp(MAP().nameMatch, 'i');
	assert.ok(re.test('DJ-Tech Mix-101'));
	assert.ok(re.test('DJ-Tech MIX-101'));
	assert.ok(re.test('MIX-101'));
	assert.ok(re.test('MIX-101 MIDI 1'));
	assert.ok(re.test('Mix101'));
	assert.ok(!re.test('DJM-101'));
	assert.ok(!re.test('DJ-Tech iMix Reload'));
	assert.ok(!re.test('Mixtour'));
	assert.ok(!re.test('Mixtrack Pro FX'));
	assert.ok(!re.test('DDJ-FLX4'));
	assert.ok(!re.test('DJConsole RMX2'));
});

test('every binding and hint source is a manufacturer chart row', () => {
	const offBinding = MAP()
		.bindings.map((b) => key(b.source))
		.filter((k) => !CHART.has(k));
	const offHint = MAP()
		.hints.map((h) => key(h.source))
		.filter((k) => !CHART.has(k));
	assert.deepEqual(offBinding, [], `off-chart bindings: ${offBinding.join(', ')}`);
	assert.deepEqual(offHint, [], `off-chart hints: ${offHint.join(', ')}`);
});

test('transport, browse_load, faders, crossfader and EQ bind from the chart', () => {
	for (const deck of DECKS) {
		const shift = deck === 2;
		const has = (kind, id, type) =>
			findBindings(
				(b) =>
					b.source.kind === kind &&
					b.source.id === id &&
					b.action.type === type &&
					b.action.deck === deck &&
					(b.shift === true) === shift
			).length === 1;
		assert.ok(has('note', 0x07, 'deck_play_toggle'), `play deck ${deck}`);
		assert.ok(has('note', 0x08, 'deck_cue'), `cue deck ${deck}`);
		assert.ok(has('note', 0x06, 'deck_sync_toggle'), `sync deck ${deck}`);
		assert.ok(has('note', 0x0c, 'browse_load'), `load deck ${deck}`);
	}
	const fader = { 1: 0x21, 2: 0x22 };
	const trim = { 1: 0x24, 2: 0x25 };
	const eqHi = { 1: 0x26, 2: 0x27 };
	const eqMid = { 1: 0x28, 2: 0x29 };
	const eqLow = { 1: 0x2a, 2: 0x2b };
	for (const deck of DECKS) {
		assert.ok(
			findBindings(
				(b) =>
					b.action.type === 'mixer_channel' &&
					b.action.deck === deck &&
					b.action.target === 'fader' &&
					b.source.id === fader[deck]
			).length === 1
		);
		assert.ok(
			findBindings(
				(b) =>
					b.action.type === 'mixer_channel' &&
					b.action.deck === deck &&
					b.action.target === 'trim' &&
					b.source.id === trim[deck]
			).length === 1
		);
		for (const [band, id] of [
			['high', eqHi[deck]],
			['mid', eqMid[deck]],
			['low', eqLow[deck]]
		]) {
			assert.ok(
				findBindings(
					(b) =>
						b.action.type === 'mixer_channel' &&
						b.action.deck === deck &&
						b.action.target === 'eq' &&
						b.action.band === band &&
						b.source.id === id
				).length === 1
			);
		}
	}
	assert.ok(
		findBindings((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader')
			.length === 1
	);
	assert.deepEqual(
		findBindings((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader')[0]
			.source,
		{ ch: 1, kind: 'cc', id: 0x23 }
	);
});

test('each deck has two relative jogs on cc 0x13 and 0x15', () => {
	for (const deck of DECKS) {
		const shift = deck === 2;
		const jogs = findBindings(
			(b) =>
				b.action.type === 'deck_jog' &&
				b.action.deck === deck &&
				(b.shift === true) === shift &&
				(b.source.id === 0x13 || b.source.id === 0x15)
		);
		assert.equal(jogs.length, 2, `jog count deck ${deck}`);
		for (const jog of jogs) {
			assert.equal(jog.relative, true);
			assert.equal(jog.relativeCentered, true);
		}
	}
});

test('A/B is the only shift_modifier; every shift:true binding targets deck 2', () => {
	const mods = findBindings((b) => b.action.type === 'shift_modifier');
	assert.equal(mods.length, 1);
	assert.deepEqual(mods[0].source, { ch: 1, kind: 'note', id: 0x09 });
	const shifted = findBindings((b) => b.shift === true);
	assert.ok(shifted.length > 0);
	for (const b of shifted) {
		assert.ok('deck' in b.action, JSON.stringify(b.action));
		assert.equal(b.action.deck, 2);
	}
});

test('FX (#9788): MODE and keys bind as controller_pad mode/pads; no beat/colour fx action', () => {
	const fxTypes = findBindings(
		(b) => b.action.type.includes('fx') && b.action.type !== 'controller_pad_mode'
	);
	assert.deepEqual(fxTypes, []);
	const modeMap = {
		0x11: 'sampler',
		0x12: 'auto_loop',
		0x13: 'instant_fx',
		0x14: 'hot_cue'
	};
	for (const deck of DECKS) {
		const shift = deck === 2;
		for (const [id, mode] of Object.entries(modeMap)) {
			const hits = findBindings(
				(b) =>
					b.action.type === 'controller_pad_mode' &&
					b.action.deck === deck &&
					b.action.mode === mode &&
					b.source.id === Number(id) &&
					(b.shift === true) === shift
			);
			assert.equal(hits.length, 1, `mode ${mode} deck ${deck}`);
		}
		for (const [noteId, pad] of [
			[0x03, 1],
			[0x04, 2]
		]) {
			const hits = findBindings(
				(b) =>
					b.action.type === 'controller_pad' &&
					b.action.deck === deck &&
					b.action.pad === pad &&
					b.action.shifted === false &&
					b.source.id === noteId &&
					(b.shift === true) === shift
			);
			assert.equal(hits.length, 1, `pad ${pad} deck ${deck}`);
		}
	}
	const instantFxMode = findBindings(
		(b) => b.action.type === 'controller_pad_mode' && b.action.mode === 'instant_fx'
	);
	assert.equal(instantFxMode.length, 2);
});

test('chart defects: one binding on cc 0x26; cc 0x23 is crossfader only', () => {
	assert.equal(bindingsAt(1, 'cc', 0x26).length, 1);
	assert.equal(bindingsAt(1, 'cc', 0x23).length, 1);
	assert.equal(bindingsAt(1, 'cc', 0x23)[0].action.target, 'crossfader');
	const hinted23 = MAP().hints.some((h) => key(h.source) === '1:cc:35');
	assert.ok(!hinted23);
});

test('two-channel map: no deck 3/4, pitch or init', () => {
	assert.deepEqual(
		findBindings((b) => 'deck' in b.action && b.action.deck > 2),
		[]
	);
	assert.deepEqual(
		findBindings((b) => b.action.type === 'deck_pitch'),
		[]
	);
	assert.equal(MAP().init, undefined);
});

test('binding keys are unique; hints never collide with unshifted bindings', () => {
	const seen = new Set();
	for (const b of MAP().bindings) {
		const k = bindingKey(b);
		assert.ok(!seen.has(k), `duplicate binding ${k}`);
		seen.add(k);
	}
	const unshiftedBound = new Set(
		MAP()
			.bindings.filter((b) => b.shift !== true)
			.map((b) => key(b.source))
	);
	const hintSeen = new Set();
	for (const h of MAP().hints) {
		const k = key(h.source);
		assert.ok(!unshiftedBound.has(k), `hint collides with binding ${k}`);
		assert.ok(!hintSeen.has(k), `hint repeats ${k}`);
		hintSeen.add(k);
	}
	for (const k of [
		'1:note:5',
		'1:note:11',
		'1:note:13',
		'1:note:80',
		'1:note:81',
		'1:note:82',
		'1:note:83',
		'1:note:84',
		'1:note:85',
		'1:cc:14',
		'1:cc:16',
		'1:cc:18'
	]) {
		assert.ok(hintSeen.has(k), `expected hint on ${k}`);
	}
});

test('LED rules use MIX101_LED_ON/OFF and expected trigger kinds', () => {
	const boundNotes = new Set(
		MAP()
			.bindings.filter((b) => b.source.kind === 'note')
			.map((b) => key(b.source))
	);
	for (const rule of MAP().leds) {
		const k = `${rule.out.ch}:note:${rule.out.note}`;
		assert.ok(boundNotes.has(k), `LED on unbound ${k}`);
		assert.equal(rule.out.velocityOn, mapModule.MIX101_LED_ON);
		assert.equal(rule.out.velocityOff, mapModule.MIX101_LED_OFF);
	}
	const kinds = new Set(MAP().leds.map((r) => r.trigger.kind));
	assert.ok(kinds.has('deck_playing'));
	assert.ok(kinds.has('deck_loaded'));
	assert.ok(kinds.has('channel_cue_enabled'));
	assert.ok(kinds.has('pad_mode_selected'));
});

test('the registry carries DJ_TECH_MIX_101_MAP with a unique nameMatch', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(MAP()));
	assert.equal(
		registry.DEVICE_MAP_REGISTRY.filter((m) => m.nameMatch === MAP().nameMatch).length,
		1
	);
});

test('device-maps.json matches the export script output', () => {
	const run = spawnSync(process.execPath, ['scripts/export-midi-maps.mjs', '--check'], {
		cwd: FRONTEND_ROOT
	});
	assert.equal(run.status, 0, String(run.stderr || run.stdout));
});
