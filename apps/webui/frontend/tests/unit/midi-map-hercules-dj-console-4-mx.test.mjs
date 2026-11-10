// Unit tests for the Hercules DJ Console 4-Mx live device map
// (src/lib/rb/midi/maps/hercules-dj-console-4-mx.ts) + maps/index.ts registry.
// Same vite+svelte bootstrap as flx10-map.test.mjs (rune modules in the
// import graph need the plugin).
//
// Every expected wire number below is read off the official Hercules
// "APPENDIX: HERCULES DJ CONSOLE 4-MX MIDI CONTROLS" ([PDF]
// https://ts.hercules.com/download/sound/manuals/4Mx/01_Eng_DJ_Console_4Mx.pdf
// Appendix 1/12–12/12). Nothing here is sourced from Mixxx or
// tools/deck-diagrams/devices/hercules-dj-console-4-mx/midi.json.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap throws then broken
//   if nameMatch misses DJ Console 4-Mx / Hercules DJ Console 4-Mx or hits RMX / Mac Edition / Mixtour then broken
//   if play/cue/sync are not the appendix notes per deck then broken
//   if VOL_DA/VOL_DB (and DC/DD) are not channel faders, or XFADER is not ch1 CC 0x22 then broken
//   if a deck jog decodes 0x01 as anything but +1 or 0x7F as anything but -1 then broken
//   if FX_* is not mixer_channel filter or FX decodes 0x01/0x7F as anything but +1/-1 then broken
//   if Load_DA/DB/DC/DD are not browse_load then broken
//   if any binding (ch, kind, id) is outside the appendix allowlist then broken
//   if a hint collides with a bound source then broken
//   if a LED rule targets a note with no MIDI-Out row then broken
//   if the map is missing from DEVICE_MAP_REGISTRY or registerAllDeviceMaps is not idempotent then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-console-4-mx.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const MAP = () => mapModule.HERCULES_DJ_CONSOLE_4MX_MAP;
const DECKS = [1, 2, 3, 4];
const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return MAP().bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

/** Delta the runtime emits for one CC data byte on a relative binding (same rule as _valueFor). */
function decodedDelta(b, raw) {
	const d =
		b.relativeCentered === true
			? webmidi.decodeRelativeCentered(raw)
			: webmidi.decodeRelative(raw);
	const unit = b.relativeUnit === true && d !== 0 ? Math.sign(d) : d;
	return b.invert === true ? -unit : unit;
}

/** [PDF] Appendix 1/12–6/12 Virtual MIDI In: notes on ch 1 and 2, CCs 0x01–0x2B on ch 1. */
function appendixAllowlist() {
	const allow = new Set();
	const add = (ch, kind, id) => allow.add(`${ch}:${kind}:${id}`);
	for (let id = 0x01; id <= 0x4b; id++) add(1, 'note', id);
	for (let id = 0x01; id <= 0x11; id++) add(2, 'note', id);
	for (let id = 0x13; id <= 0x20; id++) add(2, 'note', id);
	for (let id = 0x21; id <= 0x31; id++) add(2, 'note', id);
	for (let id = 0x33; id <= 0x3b; id++) add(2, 'note', id);
	for (let id = 0x42; id <= 0x46; id++) add(2, 'note', id);
	add(2, 'note', 0x4a);
	add(2, 'note', 0x4b);
	for (let id = 0x01; id <= 0x2b; id++) add(1, 'cc', id);
	return allow;
}

/** [PDF] Appendix 7/12–9/12: 00 OFF / 7F ON on the same note as the input. */
function midiOutNotes() {
	const allow = new Set();
	const add = (ch, note) => allow.add(`${ch}:${note}`);
	for (const ch of [1, 2]) {
		for (const id of [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x0d, 0x0e, 0x0f, 0x11]) {
			add(ch, id);
		}
		for (const id of [0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x2d, 0x2e, 0x2f, 0x31]) {
			add(ch, id);
		}
	}
	return allow;
}

const PLAY = { 1: 0x0e, 2: 0x2e, 3: 0x0e, 4: 0x2e };
const CUE = { 1: 0x0d, 2: 0x2d, 3: 0x0d, 4: 0x2d };
const SYNC = { 1: 0x11, 2: 0x31, 3: 0x11, 4: 0x31 };
const NOTE_CH = { 1: 1, 2: 1, 3: 2, 4: 2 };
const VOL = { 1: 0x03, 2: 0x0b, 3: 0x13, 4: 0x1b };
const JOG_SEEK = { 1: 0x01, 2: 0x09, 3: 0x11, 4: 0x19 };
const FX = { 1: 0x02, 2: 0x0a, 3: 0x12, 4: 0x1a };
const LOAD = { 1: 0x4a, 2: 0x4b, 3: 0x4a, 4: 0x4b };

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(MAP());
	webmidi._resetMidiForTests();
});

test('nameMatch covers DJ Console 4-Mx and not RMX, Mac Edition or Mixtour', () => {
	const re = new RegExp(MAP().nameMatch, 'i');
	assert.ok(re.test('DJ Console 4-Mx'));
	assert.ok(re.test('Hercules DJ Console 4-Mx'));
	assert.ok(re.test('Hercules DJ Console 4-Mx MIDI'));
	assert.ok(!re.test('DJ Console RMX'));
	assert.ok(!re.test('DJ Console Mac Edition'));
	assert.ok(!re.test('Reloop Mixtour'));
	assert.ok(!re.test('Pioneer DDJ-FLX10'));
});

test('play/cue/sync are the appendix notes per deck', () => {
	for (const deck of DECKS) {
		const ch = NOTE_CH[deck];
		const play = theBinding((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck);
		assert.deepEqual(play.source, { ch, kind: 'note', id: PLAY[deck] });
		const cue = theBinding((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.deepEqual(cue.source, { ch, kind: 'note', id: CUE[deck] });
		const sync = theBinding((b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck);
		assert.deepEqual(sync.source, { ch, kind: 'note', id: SYNC[deck] });
	}
});

test('VOL_DA/DB/DC/DD are channel faders and XFADER is ch1 CC 0x22', () => {
	for (const deck of DECKS) {
		const vol = theBinding(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
		);
		assert.deepEqual(vol.source, { ch: 1, kind: 'cc', id: VOL[deck] });
	}
	const xf = theBinding(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(xf.source, { ch: 1, kind: 'cc', id: 0x22 });
	assert.notEqual(xf.invert, true);
});

test("each deck jog decodes appendix Incremental bytes as two's-complement deltas", () => {
	for (const deck of DECKS) {
		const jogSeek = theBinding(
			(b) =>
				b.action.type === 'deck_jog' &&
				b.action.deck === deck &&
				b.source.kind === 'cc' &&
				b.source.id === JOG_SEEK[deck]
		);
		assert.equal(jogSeek.source.ch, 1);
		assert.equal(jogSeek.relative, true);
		const jogs = findBindings((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.equal(jogs.length, 2, `deck ${deck} should have JOG_SEEK and JOG_SCR`);
		for (const b of jogs) {
			assert.equal(decodedDelta(b, 0x01), 1);
			assert.equal(decodedDelta(b, 0x7f), -1);
			assert.equal(decodedDelta(b, 0x3f), 63);
			assert.equal(decodedDelta(b, 0x40), -64);
		}
	}
});

test('FX_* binds mixer_channel filter and decodes 0x01/0x7F as +1/-1', () => {
	for (const deck of DECKS) {
		const fx = theBinding(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'filter'
		);
		assert.deepEqual(fx.source, { ch: 1, kind: 'cc', id: FX[deck] });
		assert.equal(fx.relative, true);
		assert.equal(decodedDelta(fx, 0x01), 1);
		assert.equal(decodedDelta(fx, 0x7f), -1);
		assert.equal(decodedDelta(fx, 0x3f), 63);
		assert.equal(decodedDelta(fx, 0x40), -64);
	}
});

test('Load_DA/DB/DC/DD bind browse_load', () => {
	for (const deck of DECKS) {
		const load = theBinding((b) => b.action.type === 'browse_load' && b.action.deck === deck);
		assert.deepEqual(load.source, { ch: NOTE_CH[deck], kind: 'note', id: LOAD[deck] });
	}
});

test('every binding source is in the appendix Virtual MIDI In table', () => {
	const allow = appendixAllowlist();
	for (const b of MAP().bindings) {
		assert.ok(allow.has(key(b.source)), `binding ${key(b.source)} is absent from the appendix`);
	}
});

test('hints never collide with a bound source', () => {
	const bound = new Set(MAP().bindings.map((b) => key(b.source)));
	const hinted = new Set();
	for (const h of MAP().hints) {
		assert.ok(!bound.has(key(h.source)), `hint ${h.label} shadows a binding`);
		assert.ok(!hinted.has(key(h.source)), `duplicate hint source ${h.label}`);
		hinted.add(key(h.source));
	}
});

test('LED rules target only notes with a printed MIDI-Out 00/7F row', () => {
	const allowed = midiOutNotes();
	assert.ok(MAP().leds.length >= 40);
	for (const rule of MAP().leds) {
		assert.ok(
			allowed.has(`${rule.out.ch}:${rule.out.note}`),
			`LED on ${rule.out.ch}:${rule.out.note}`
		);
		assert.equal(rule.out.velocityOn, 0x7f);
		assert.equal(rule.out.velocityOff, 0x00);
	}
});

test('registry includes the 4-Mx map and registerAllDeviceMaps is idempotent', () => {
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	const names = registry.DEVICE_MAP_REGISTRY.map((m) => m.nameMatch);
	assert.ok(names.includes('DJ Console 4-Mx'));
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(MAP()));
	registry.registerAllDeviceMaps();
	registry.registerAllDeviceMaps();
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});
