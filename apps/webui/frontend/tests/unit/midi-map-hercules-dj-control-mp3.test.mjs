// requirement: MIDI-11965
// Unit tests for Hercules DJ Control MP3 (src/lib/rb/midi/maps/hercules-dj-control-mp3.ts).
//
// Regression lines:
//   if registerDeviceMap(HERCULES_DJ_CONTROL_MP3_MAP) throws then broken
//   if nameMatch matches e2, LE, Glow or DDJ-FLX4 then broken
//   if binding or hint source is not ch1 CC from manual section 6 then broken
//   if play/cue/sync lack ccButton or jog lacks relative then broken
//   if FX chart ids are bound (MidiAction has no FX action) then broken
//   if deck_pitch or deck_tempo_nudge is bound then broken
//   if hints collide with bindings or repeat then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-control-mp3.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return mapModule.HERCULES_DJ_CONTROL_MP3_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const DECKS = [1, 2];
const FX_IDS = new Set([0x07, 0x0f, 0x0e, 0x0d, 0x01, 0x10, 0x11, 0x12]);

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.HERCULES_DJ_CONTROL_MP3_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers original MP3 ports only (no look-around)', () => {
	const { nameMatch } = mapModule.HERCULES_DJ_CONTROL_MP3_MAP;
	const re = new RegExp(nameMatch, 'i');
	assert.ok(re.test('Hercules DJ Control MP3 MIDI'));
	assert.ok(re.test('Hercules DJ Control MP3 MIDI 1'));
	assert.ok(!re.test('Hercules DJ Control MP3 e2'));
	assert.ok(!re.test('Hercules DJ Control MP3 e2 MIDI'));
	assert.ok(!re.test('Hercules DJ Control MP3 LE MIDI'));
	assert.ok(!re.test('DJControl Glow'));
	assert.ok(!re.test('DDJ-FLX4'));
	for (const bad of ['(?=', '(?!', '(?<=', '(?<!']) {
		assert.ok(!nameMatch.includes(bad), `nameMatch must not contain ${bad}`);
	}
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	const resolved = webmidi.resolveMapForPort('Hercules DJ Control MP3 MIDI 1');
	assert.equal(resolved, mapModule.HERCULES_DJ_CONTROL_MP3_MAP);
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('every binding and hint is channel 1 CC; chart ids 1..28 and 43..57 exactly once', () => {
	const all = [
		...mapModule.HERCULES_DJ_CONTROL_MP3_MAP.bindings.map((b) => b.source),
		...mapModule.HERCULES_DJ_CONTROL_MP3_MAP.hints.map((h) => h.source)
	];
	const ids = [];
	for (const s of all) {
		assert.equal(s.ch, 1);
		assert.equal(s.kind, 'cc');
		ids.push(s.id);
	}
	assert.equal(ids.length, 43);
	const expected = new Set();
	for (let i = 1; i <= 28; i++) expected.add(i);
	for (let i = 43; i <= 57; i++) expected.add(i);
	assert.deepEqual(new Set(ids), expected);
});

test('required transport, fader, crossfader and jog wires per deck', () => {
	const deck1 = {
		play: 0x08,
		cue: 0x09,
		sync: 0x0a,
		fader: 0x32,
		jog: 0x36
	};
	const deck2 = {
		play: 0x02,
		cue: 0x03,
		sync: 0x04,
		fader: 0x33,
		jog: 0x37
	};
	for (const [deck, w] of [
		[1, deck1],
		[2, deck2]
	]) {
		const play = theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck);
		assert.equal(play.source.id, w.play);
		assert.equal(play.ccButton, true);
		const cue = theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.equal(cue.source.id, w.cue);
		assert.equal(cue.ccButton, true);
		const sync = theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck);
		assert.equal(sync.source.id, w.sync);
		assert.equal(sync.ccButton, true);
		const fader = theBinding(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
		);
		assert.equal(fader.source.id, w.fader);
		const jog = theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.equal(jog.source.id, w.jog);
		assert.equal(jog.relative, true);
		assert.notEqual(jog.relativeCentered, true);
	}
	const xf = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.equal(xf.source.id, 0x31);
});

test('EQ, PFL and ccButton on all button bindings', () => {
	const eq1 = { low: 0x2e, mid: 0x2f, high: 0x30 };
	const eq2 = { low: 0x2b, mid: 0x2c, high: 0x2d };
	for (const [deck, eq] of [
		[1, eq1],
		[2, eq2]
	]) {
		for (const band of ['low', 'mid', 'high']) {
			const row = theBinding(
				(b) =>
					b.action.type === 'mixer_channel' &&
					b.action.deck === deck &&
					b.action.target === 'eq' &&
					b.action.band === band
			);
			assert.equal(row.source.id, eq[band]);
		}
		const pfl = theBinding((b) => b.action.type === 'channel_cue' && b.action.deck === deck);
		assert.equal(pfl.ccButton, true);
		assert.equal(pfl.source.id, deck === 1 ? 0x15 : 0x19);
	}
	for (const b of mapModule.HERCULES_DJ_CONTROL_MP3_MAP.bindings) {
		if (b.source.id <= 28) {
			assert.equal(b.ccButton, true, `button CC ${b.source.id} needs ccButton`);
		} else {
			assert.notEqual(b.ccButton, true, `analog CC ${b.source.id} must not be ccButton`);
		}
	}
});

test('FX chart rows are hints only (MidiAction has no beat-FX or colour-FX action)', () => {
	for (const b of mapModule.HERCULES_DJ_CONTROL_MP3_MAP.bindings) {
		assert.ok(!FX_IDS.has(b.source.id), `FX id ${b.source.id} must not be bound`);
	}
	const fxHints = mapModule.HERCULES_DJ_CONTROL_MP3_MAP.hints.filter((h) =>
		FX_IDS.has(h.source.id)
	);
	assert.equal(fxHints.length, 8);
	for (const h of fxHints) {
		assert.ok(h.label.includes('FX'), `FX hint label must contain FX: ${h.label}`);
	}
});

test('pitch knobs and pitch bend are hints; no deck_pitch or deck_tempo_nudge', () => {
	const pitchIds = new Set([0x34, 0x35, 0x13, 0x14, 0x17, 0x18]);
	const hints = mapModule.HERCULES_DJ_CONTROL_MP3_MAP.hints.filter((h) =>
		pitchIds.has(h.source.id)
	);
	assert.equal(hints.length, 6);
	assert.deepEqual(
		findBindings((b) => b.action.type === 'deck_pitch'),
		[]
	);
	assert.deepEqual(
		findBindings((b) => b.action.type === 'deck_tempo_nudge'),
		[]
	);
});

test('two-deck scope, no LEDs, unique hints', () => {
	assert.deepEqual(
		findBindings((b) => 'deck' in b.action && (b.action.deck === 3 || b.action.deck === 4)),
		[]
	);
	assert.deepEqual(mapModule.HERCULES_DJ_CONTROL_MP3_MAP.leds, []);
	const bound = new Set(mapModule.HERCULES_DJ_CONTROL_MP3_MAP.bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of mapModule.HERCULES_DJ_CONTROL_MP3_MAP.hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint ${h.label} collides with binding ${k}`);
		assert.ok(!seen.has(k), `hint ${h.label} repeats ${k}`);
		seen.add(k);
	}
});

test('registry and DEVICE_MAP_ENTRIES carry HERCULES_DJ_CONTROL_MP3_MAP', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(mapModule.HERCULES_DJ_CONTROL_MP3_MAP));
	const entry = mapModule.DEVICE_MAP_ENTRIES.find((e) => e.key === 'HERCULES_DJ_CONTROL_MP3_MAP');
	assert.ok(entry);
	assert.equal(entry.live, true);
	assert.equal(entry.map, mapModule.HERCULES_DJ_CONTROL_MP3_MAP);
});
