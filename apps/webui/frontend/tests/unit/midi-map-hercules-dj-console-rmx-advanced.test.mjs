// Unit tests for the Hercules DJ Console RMX Advanced live device map
// (src/lib/rb/midi/maps/hercules-dj-console-rmx-advanced.ts). Same
// vite+svelte bootstrap as mixtour-map.test.mjs (rune modules in the
// import graph need the plugin).
//
// Every expected wire number is read off the manufacturer sent table
// ([MAN] User Manual section 10, pages 24-25). Nothing here is sourced
// from Mixxx XML or tools/deck-diagrams midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP) throws then broken
//   if nameMatch accepts DJ Console RMX2 or DDJ-FLX4 then broken
//   if play/cue/sync aren't cc ch1 0x0B/0x0C/0x07 and 0x23/0x24/0x1F ([MAN]) then broken
//   if a transport/load/PFL/pad button lacks ccButton then broken
//   if channel faders aren't 0x32/0x3C or the crossfader isn't 0x39 ([MAN]) then broken
//   if jogs aren't 0x2F/0x30 relative two's-complement ([MAN]) then broken
//   if keypads 1-6 per deck aren't controller_pad on 0x01..0x06 / 0x19..0x1E then broken
//   if load isn't browse_load on 0x12/0x16 then broken
//   if a binding uses a (ch, kind, id) outside the sent table 0x01..0x40 cc ch1 then broken
//   if hints collide with bound sources then broken
//   if DEVICE_MAP_ENTRIES does not register this map live then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-console-rmx-advanced.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const MAP_KEY = 'HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP';
const BUTTON_TYPES = new Set([
	'deck_play_toggle',
	'deck_cue',
	'deck_sync_toggle',
	'channel_cue',
	'browse_load',
	'controller_pad'
]);
/** [MAN] sent table pages 24-25: every row is Bx <cc> with cc 0x01..0x40, no gaps. */
const SENT_CCS = new Set(Array.from({ length: 0x40 }, (_, i) => i + 1));

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return mapModule.HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers DJ Console RMX / Advanced and excludes RMX2 and FLX4', () => {
	const re = new RegExp(mapModule.HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP.nameMatch, 'i');
	assert.ok(re.test('DJ Console RMX'));
	assert.ok(re.test('DJ Console Rmx'));
	assert.ok(re.test('Hercules DJ Console RMX'));
	assert.ok(re.test('DJ Console RMX Advanced'));
	assert.ok(re.test('DJ Console RMX MIDI 1'));
	assert.ok(!re.test('DJ Console RMX2'));
	assert.ok(!re.test('Hercules DJ Console RMX2'));
	assert.ok(!re.test('DDJ-FLX4'));
});

test('transport: play/cue/sync per deck on the [MAN] CCs, ccButton', () => {
	const rows = [
		[1, 0x0b, 0x0c, 0x07],
		[2, 0x23, 0x24, 0x1f]
	];
	for (const [deck, playId, cueId, syncId] of rows) {
		const play = theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck);
		assert.deepEqual(play.source, { ch: 1, kind: 'cc', id: playId });
		assert.equal(play.ccButton, true);
		const cue = theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.deepEqual(cue.source, { ch: 1, kind: 'cc', id: cueId });
		assert.equal(cue.ccButton, true);
		const sync = theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck);
		assert.deepEqual(sync.source, { ch: 1, kind: 'cc', id: syncId });
		assert.equal(sync.ccButton, true);
	}
});

test('mixer: channel faders 0x32/0x3C and crossfader 0x39 ([MAN] p.25)', () => {
	const left = theBinding(
		(b) => b.action.type === 'mixer_channel' && b.action.deck === 1 && b.action.target === 'fader'
	);
	assert.deepEqual(left.source, { ch: 1, kind: 'cc', id: 0x32 });
	const right = theBinding(
		(b) => b.action.type === 'mixer_channel' && b.action.deck === 2 && b.action.target === 'fader'
	);
	assert.deepEqual(right.source, { ch: 1, kind: 'cc', id: 0x3c });
	const xf = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(xf.source, { ch: 1, kind: 'cc', id: 0x39 });
});

test("jogs: 0x2F/0x30 relative two's-complement, not centre-64 ([MAN] p.25)", () => {
	for (const [deck, id] of [
		[1, 0x2f],
		[2, 0x30]
	]) {
		const jog = theBinding((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.deepEqual(jog.source, { ch: 1, kind: 'cc', id });
		assert.equal(jog.relative, true);
		assert.notEqual(jog.relativeCentered, true);
		assert.notEqual(jog.relativeUnit, true);
	}
});

test('keypads 1-6 per deck are controller_pad on [MAN] 0x01..0x06 / 0x19..0x1E', () => {
	for (const [deck, first] of [
		[1, 0x01],
		[2, 0x19]
	]) {
		const pads = findBindings((b) => b.action.type === 'controller_pad' && b.action.deck === deck);
		assert.equal(pads.length, 6);
		for (const pad of [1, 2, 3, 4, 5, 6]) {
			const b = pads.find((row) => row.action.pad === pad);
			assert.ok(b, `missing keypad ${pad} deck ${deck}`);
			assert.deepEqual(b.source, { ch: 1, kind: 'cc', id: first + pad - 1 });
			assert.equal(b.action.shifted, false);
			assert.equal(b.ccButton, true);
		}
	}
});

test('load: browse_load on 0x12 / 0x16 ([MAN] p.24)', () => {
	for (const [deck, id] of [
		[1, 0x12],
		[2, 0x16]
	]) {
		const load = theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck);
		assert.deepEqual(load.source, { ch: 1, kind: 'cc', id });
		assert.equal(load.ccButton, true);
	}
});

test('EQ / trim / pitch / master / cue mix / PFL match the [MAN] table', () => {
	const strip = (deck, ids) => {
		const rows = findBindings((b) => b.action.type === 'mixer_channel' && b.action.deck === deck);
		const byId = Object.fromEntries(rows.map((b) => [b.source.id, b.action]));
		assert.equal(byId[ids.trim].target, 'trim');
		assert.equal(byId[ids.high].target, 'eq');
		assert.equal(byId[ids.high].band, 'high');
		assert.equal(byId[ids.mid].band, 'mid');
		assert.equal(byId[ids.low].band, 'low');
	};
	strip(1, { trim: 0x33, high: 0x34, mid: 0x35, low: 0x36 });
	strip(2, { trim: 0x3d, high: 0x3e, mid: 0x3f, low: 0x40 });
	for (const [deck, id] of [
		[1, 0x31],
		[2, 0x3b]
	]) {
		const pitch = theBinding((b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
		assert.deepEqual(pitch.source, { ch: 1, kind: 'cc', id });
		assert.equal(pitch.action.lsbOffset, null);
		assert.notEqual(pitch.invert, true);
	}
	const master = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'master'
	);
	assert.deepEqual(master.source, { ch: 1, kind: 'cc', id: 0x38 });
	const cueMix = theBinding((b) => b.action.type === 'headphone_mix');
	assert.deepEqual(cueMix.source, { ch: 1, kind: 'cc', id: 0x3a });
	for (const [deck, id] of [
		[1, 0x14],
		[2, 0x18]
	]) {
		const pfl = theBinding((b) => b.action.type === 'channel_cue' && b.action.deck === deck);
		assert.deepEqual(pfl.source, { ch: 1, kind: 'cc', id });
		assert.equal(pfl.ccButton, true);
	}
});

test('every binding is cc ch1 in the sent table; every button has ccButton', () => {
	const map = mapModule.HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP;
	for (const b of map.bindings) {
		assert.equal(b.source.ch, 1);
		assert.equal(b.source.kind, 'cc');
		assert.ok(
			SENT_CCS.has(b.source.id),
			`id 0x${b.source.id.toString(16)} is not in the sent table`
		);
		if (BUTTON_TYPES.has(b.action.type)) {
			assert.equal(
				b.ccButton,
				true,
				`${b.action.type} on 0x${b.source.id.toString(16)} lacks ccButton`
			);
		}
	}
});

test('hints are unique and never collide with bound sources', () => {
	const map = mapModule.HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP;
	const bound = new Set(map.bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of map.hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint ${h.label} collides with a binding on ${k}`);
		assert.ok(!seen.has(k), `hint ${h.label} repeats ${k}`);
		seen.add(k);
		assert.equal(h.source.ch, 1);
		assert.equal(h.source.kind, 'cc');
		assert.ok(SENT_CCS.has(h.source.id), `hint ${h.label} id is not in the sent table`);
		assert.ok(h.label.length > 0);
	}
});

test('map is live in DEVICE_MAP_ENTRIES and the registry', () => {
	assert.equal(mapModule.DEVICE_MAP_ENTRIES.length, 1);
	assert.equal(mapModule.DEVICE_MAP_ENTRIES[0].key, MAP_KEY);
	assert.equal(mapModule.DEVICE_MAP_ENTRIES[0].live, true);
	assert.equal(mapModule.DEVICE_MAP_ENTRIES[0].map, mapModule.HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP);
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(mapModule.HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP));
});
