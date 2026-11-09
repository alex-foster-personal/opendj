// requirement: SKIN-10487 (Denon DJ PRIME 4+ practice replica, epic #9366)
// The PRIME 4+ practice panel joins to DENON_PRIME4_PLUS_MAP, a hints-only map: Denon DJ
// publishes no MIDI message list, so every control is inert with the PARITY-TODO title.
//
//   if any panel control's wire identity is not hinted by DENON_PRIME4_PLUS_MAP then broken
//   if the map binds anything, lights anything, or hints a wire off the placeholder channel then broken
//   if any panel control resolves as bound (a fake behaviour) then broken
//   if DENON_PRIME4_PLUS_MAP is missing from PRACTICE_DEVICE_MAPS then broken
//   if DENON_PRIME4_PLUS_MAP is registered in DEVICE_MAP_REGISTRY (placeholders meeting real traffic) then broken
//   if validateDeviceMap(DENON_PRIME4_PLUS_MAP) throws then broken
//   if two hints share one source then broken
//   if nameMatch misses "PRIME 4+" or matches "PRIME 4" or "PRIME GO+" then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(
	FRONTEND_ROOT,
	'../../../tools/deck-diagrams/devices/denon-prime4-plus/panel.json'
);
const CONTROL_COUNT = 170;

let vite;
let bindings;
let DENON_PRIME4_PLUS_MAP;
let DENON_PRIME4_PLUS_PLACEHOLDER_CH;
let registry;
let devices;
let webmidi;
const layout = JSON.parse(readFileSync(PANEL, 'utf8'));

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: { alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') }, conditions: ['browser'] }
	});
	bindings = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-bindings.ts');
	({ DENON_PRIME4_PLUS_MAP, DENON_PRIME4_PLUS_PLACEHOLDER_CH } = await vite.ssrLoadModule(
		'/src/lib/rb/midi/maps/denon-prime4-plus.ts'
	));
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
	devices = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-devices.ts');
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

test('every PRIME 4+ panel control resolves against the map, and every one is inert', () => {
	const resolved = bindings.resolveLayout(DENON_PRIME4_PLUS_MAP, layout);
	assert.equal(resolved.length, CONTROL_COUNT);
	for (const r of resolved) {
		assert.equal(r.kind, 'inert', r.control.id);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/);
	}
	assert.ok(layout.controls.every((c) => c.shift_midi === null));
});

test('the map binds nothing and lights nothing: no wire is documented', () => {
	assert.deepEqual(DENON_PRIME4_PLUS_MAP.bindings, []);
	assert.deepEqual(DENON_PRIME4_PLUS_MAP.leds, []);
	assert.equal(DENON_PRIME4_PLUS_MAP.meters, undefined);
	assert.equal(DENON_PRIME4_PLUS_MAP.hints.length, CONTROL_COUNT);
	assert.equal(DENON_PRIME4_PLUS_PLACEHOLDER_CH, 16);
	for (const h of DENON_PRIME4_PLUS_MAP.hints) assert.equal(h.source.ch, 16, h.label);
	const sources = DENON_PRIME4_PLUS_MAP.hints.map((h) => key(h.source));
	assert.equal(new Set(sources).size, sources.length, 'duplicate hint source');
	webmidi.validateDeviceMap(DENON_PRIME4_PLUS_MAP);
});

test('registered for the practice route only, never for hardware', () => {
	assert.equal(devices.PRACTICE_DEVICE_MAPS.DENON_PRIME4_PLUS_MAP, DENON_PRIME4_PLUS_MAP);
	assert.equal(devices.practiceMapFor(layout), DENON_PRIME4_PLUS_MAP);
	assert.ok(
		!registry.DEVICE_MAP_REGISTRY.includes(DENON_PRIME4_PLUS_MAP),
		'placeholder wires must never meet real MIDI traffic'
	);
});

test('hint labels name the control the panel draws', () => {
	const byKey = new Map(DENON_PRIME4_PLUS_MAP.hints.map((h) => [key(h.source), h.label]));
	const play = layout.controls.find((c) => c.id === 'd1-play');
	assert.equal(byKey.get(key(play.midi)), 'PLAY/PAUSE (deck 1)');
	const sweep = layout.controls.find((c) => c.id === 'ch4-sweep-fx');
	assert.equal(byKey.get(key(sweep.midi)), 'SWEEP FX (channel 4)');
	const browse = layout.controls.find((c) => c.id === 'browse');
	assert.equal(byKey.get(key(browse.midi)), 'BROWSE');
});

test('nameMatch picks the PRIME 4+ and nothing else in the family', () => {
	const re = new RegExp(DENON_PRIME4_PLUS_MAP.nameMatch, 'i');
	assert.ok(re.test('PRIME 4+'));
	assert.ok(re.test('Denon DJ PRIME 4+ MIDI 1'));
	assert.ok(!re.test('PRIME 4'));
	assert.ok(!re.test('PRIME 4 MIDI 1'));
	assert.ok(!re.test('PRIME GO+'));
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x0b } };
	assert.throws(
		() => bindings.resolveControl(DENON_PRIME4_PLUS_MAP, bogus),
		/neither binds nor hints/
	);
});
