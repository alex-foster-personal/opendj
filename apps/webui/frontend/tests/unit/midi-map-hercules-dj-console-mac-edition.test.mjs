// Unit tests for the Hercules DJ Console Mac Edition live device map
// (src/lib/rb/midi/maps/hercules-dj-console-mac-edition.ts). Same vite+svelte
// bootstrap as mixtour-map.test.mjs (rune modules in the import graph need the
// plugin). Never read .svelte source as text.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(HERCULES_DJ_CONSOLE_MAC_EDITION_MAP) throws then broken
//   if nameMatch misses Hercules DJ Console or Hercules DJ Console Mac Edition then broken
//   if nameMatch accepts Mk2 / Mk4 / 4-Mx / RMX then the sibling SKU collision is back
//   if play/cue/sync/faders/crossfader/jogs are not the [PDF] §7 CC ids then broken
//   if a binding's (ch, kind, id) is absent from the [PDF] §7 input table then fabricated
//   if FX Select / FX 1-3 are bindings instead of hints then the P0 FX fence leaked

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-console-mac-edition.ts');
});

after(async () => {
	await vite.close();
});

function findBindings(pred) {
	return mapModule.HERCULES_DJ_CONSOLE_MAC_EDITION_MAP.bindings.filter(pred);
}

/** [PDF] §7 MIDI input rows that this map is allowed to bind (channel 1 CC). */
const ALLOWED_BINDING_KEYS = new Set(
	[
		0x08, 0x09, 0x0a, 0x13, 0x14, 0x16, 0x21, 0x02, 0x03, 0x04, 0x17, 0x18, 0x1a, 0x22, 0x31, 0x32,
		0x33, 0x2e, 0x2f, 0x30, 0x2b, 0x2c, 0x2d, 0x36, 0x37
	].map((id) => `1:cc:${id}`)
);

const FX_HINT_IDS = [0x07, 0x01, 0x0f, 0x0e, 0x0d, 0x10, 0x11, 0x12];

// -------------------------------------------------------------- validation

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.HERCULES_DJ_CONSOLE_MAC_EDITION_MAP);
	webmidi._resetMidiForTests();
});

test('DEVICE_MAP_ENTRIES is the live self-registration of this map', () => {
	assert.equal(mapModule.DEVICE_MAP_ENTRIES.length, 1);
	const entry = mapModule.DEVICE_MAP_ENTRIES[0];
	assert.equal(entry.key, 'HERCULES_DJ_CONSOLE_MAC_EDITION_MAP');
	assert.equal(entry.live, true);
	assert.equal(entry.map, mapModule.HERCULES_DJ_CONSOLE_MAC_EDITION_MAP);
});

test('nameMatch covers FAQ USB and catalog names and excludes sibling SKUs', () => {
	const re = new RegExp(mapModule.HERCULES_DJ_CONSOLE_MAC_EDITION_MAP.nameMatch, 'i');
	assert.ok(re.test('Hercules DJ Console'));
	assert.ok(re.test('Hercules DJ Console Mac Edition'));
	assert.ok(re.test('Hercules DJ Console Port 1'));
	assert.ok(re.test('Hercules DJ Console MIDI 1'));
	assert.ok(!re.test('Hercules DJ Console Mk2'));
	assert.ok(!re.test('Hercules DJ Console Mk4'));
	assert.ok(!re.test('Hercules DJ Console 4-Mx'));
	assert.ok(!re.test('Hercules DJ Console RMX'));
	assert.ok(!re.test('DDJ-FLX10'));
});

// ------------------------------------------------------- cited spot checks

test('transport: play/cue/sync per deck on CC 0x08/09/0A and 0x02/03/04 ch1 ([PDF] §7)', () => {
	for (const [id, deck] of [
		[0x08, 1],
		[0x02, 2]
	]) {
		const play = findBindings(
			(b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck
		);
		assert.equal(play.length, 1);
		assert.deepEqual(play[0].source, { ch: 1, kind: 'cc', id });
		assert.equal(play[0].ccButton, true);
	}
	for (const [id, deck] of [
		[0x09, 1],
		[0x03, 2]
	]) {
		const cue = findBindings((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.equal(cue.length, 1);
		assert.deepEqual(cue[0].source, { ch: 1, kind: 'cc', id });
		assert.equal(cue[0].ccButton, true);
	}
	for (const [id, deck] of [
		[0x0a, 1],
		[0x04, 2]
	]) {
		const sync = findBindings(
			(b) => b.action.type === 'deck_sync_toggle' && b.action.deck === deck
		);
		assert.equal(sync.length, 1);
		assert.deepEqual(sync[0].source, { ch: 1, kind: 'cc', id });
		assert.equal(sync[0].ccButton, true);
	}
});

test('mixer: channel faders CC 0x32/0x33 and crossfader CC 0x31 ([PDF] §7)', () => {
	for (const [id, deck] of [
		[0x32, 1],
		[0x33, 2]
	]) {
		const fader = findBindings(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
		);
		assert.equal(fader.length, 1);
		assert.deepEqual(fader[0].source, { ch: 1, kind: 'cc', id });
	}
	const xf = findBindings(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.equal(xf.length, 1);
	assert.deepEqual(xf[0].source, { ch: 1, kind: 'cc', id: 0x31 });
});

test('jogs: deck 1 CC 0x36 and deck 2 CC 0x37 relativeCentered ([PDF] §7)', () => {
	for (const [id, deck] of [
		[0x36, 1],
		[0x37, 2]
	]) {
		const jog = findBindings((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.equal(jog.length, 1);
		assert.deepEqual(jog[0].source, { ch: 1, kind: 'cc', id });
		assert.equal(jog[0].relative, true);
		assert.equal(jog[0].relativeCentered, true);
	}
});

test('every binding (ch, kind, id) is on the [PDF] §7 input table', () => {
	for (const b of mapModule.HERCULES_DJ_CONSOLE_MAC_EDITION_MAP.bindings) {
		const key = `${b.source.ch}:${b.source.kind}:${b.source.id}`;
		assert.ok(ALLOWED_BINDING_KEYS.has(key), `binding ${key} is not on the [PDF] §7 input table`);
	}
});

test('FX Select and FX 1-3 are hints, not bindings ([PDF] §7; no beat-FX MidiAction)', () => {
	const bindings = mapModule.HERCULES_DJ_CONSOLE_MAC_EDITION_MAP.bindings;
	const hints = mapModule.HERCULES_DJ_CONSOLE_MAC_EDITION_MAP.hints;
	for (const id of FX_HINT_IDS) {
		assert.equal(
			bindings.filter((b) => b.source.kind === 'cc' && b.source.id === id).length,
			0,
			`FX CC 0x${id.toString(16)} must not be a binding`
		);
		assert.equal(
			hints.filter((h) => h.source.kind === 'cc' && h.source.id === id).length,
			1,
			`FX CC 0x${id.toString(16)} must be a hint`
		);
	}
});

test('map has no LED rules (sheet LEDs are CC output; LedRule is notes)', () => {
	const leds = mapModule.HERCULES_DJ_CONSOLE_MAC_EDITION_MAP.leds;
	assert.ok(leds === undefined || leds.length === 0);
});
