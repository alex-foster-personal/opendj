// requirement: SKIN-10492
// The Traktor Kontrol S4 MK3 practice panel joins to TRAKTOR_S4_MK3_MAP: the unit speaks HID,
// Native Instruments publishes no MIDI list, so every control declares midi null and resolves unwired.
//
//   if TRAKTOR_S4_MK3_MAP has any binding, LED rule or meter then broken
//   if the map carries hints then broken
//   if any panel control carries a wire number or resolves as anything but unwired then broken
//   if the hover title of a control omits PARITY-TODO or a press dispatches anything then broken
//   if practiceMapFor(panel) does not return TRAKTOR_S4_MK3_MAP then broken
//   if DEVICE_MAP_REGISTRY carries the map, or any registry map matches the S4 MK3 name then broken
//   if a control with a key absent from the map resolves silently then broken
//   if a control with midi null but shift_midi set does not throw then broken

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
	'../../../tools/deck-diagrams/devices/traktor-s4-mk3/panel.json'
);

let vite;
let bindings;
let devices;
let registry;
let webmidi;
let mapModule;
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
	devices = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-devices.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/traktor-s4-mk3.ts');
});

after(async () => {
	await vite.close();
});

test('the map is bindings-free and hint-free', () => {
	const map = mapModule.TRAKTOR_S4_MK3_MAP;
	assert.equal(map.vendor, 'Native Instruments');
	assert.deepEqual(map.bindings, []);
	assert.equal(map.hints, undefined);
	assert.equal(map.leds, undefined);
	assert.equal(map.meters, undefined);
	// It is still a valid DeviceMap for the core's fail-fast validator.
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(map);
	webmidi._resetMidiForTests();
});

test('every panel control declares its wire unavailable and resolves unwired', () => {
	const map = mapModule.TRAKTOR_S4_MK3_MAP;
	assert.equal(layout.device_map, 'TRAKTOR_S4_MK3_MAP');
	assert.equal(layout.controls.length, 139);
	assert.ok(!JSON.stringify(layout.controls).includes('"ch"'));
	for (const c of layout.controls) {
		assert.equal(c.midi, null, `${c.id} midi`);
		assert.equal(c.shift_midi, null, `${c.id} shift_midi`);
	}
	const resolved = bindings.resolveLayout(map, layout);
	assert.equal(resolved.length, layout.controls.length);
	for (const r of resolved) {
		assert.equal(r.kind, 'unwired', `${r.control.id} resolved ${r.kind}`);
		assert.equal(r.wire, null, `${r.control.id} wire`);
		assert.equal(bindings.resolveControl(map, r.control, true).kind, 'unwired');
	}
});

test('hover titles say PARITY-TODO and presses dispatch nothing', () => {
	const map = mapModule.TRAKTOR_S4_MK3_MAP;
	const play = layout.controls.find((c) => c.id === 'd1-play-pause');
	const r = bindings.resolveControl(map, play);
	assert.match(bindings.controlTitle(r), /PARITY-TODO/);
	assert.match(bindings.controlTitle(r), /PLAY\/PAUSE/);
	assert.match(bindings.controlTitle(r), /no MIDI identity/);
	const calls = [];
	const sinks = { action: (...a) => calls.push(a), shift: (...a) => calls.push(a) };
	assert.equal(bindings.dispatchPractice(r, { pressed: true }, sinks), 'inert');
	assert.deepEqual(calls, []);
	const tempo = layout.controls.find((c) => c.id === 'd1-tempo');
	const rt = bindings.resolveControl(map, tempo);
	assert.equal(bindings.dispatchPractice(rt, { value01: 0.5 }, sinks), 'inert');
	assert.deepEqual(calls, []);
});

test('the practice registry serves the map; the WebMIDI registry never carries it', () => {
	const map = mapModule.TRAKTOR_S4_MK3_MAP;
	assert.equal(devices.practiceMapFor(layout), map);
	assert.equal(devices.PRACTICE_DEVICE_MAPS.TRAKTOR_S4_MK3_MAP, map);
	assert.ok(!registry.DEVICE_MAP_REGISTRY.includes(map));
	for (const m of registry.DEVICE_MAP_REGISTRY) {
		assert.ok(
			!new RegExp(m.nameMatch, 'i').test('Traktor Kontrol S4 MK3'),
			`${m.nameMatch} would claim the S4 MK3 port name`
		);
	}
});

test('a control whose key the map lacks throws (layout and map disagree)', () => {
	const map = mapModule.TRAKTOR_S4_MK3_MAP;
	const stray = { ...layout.controls[0], id: 'stray', midi: { ch: 15, kind: 'note', id: 127 } };
	assert.throws(() => bindings.resolveControl(map, stray), /neither binds nor hints/);
});

test('a control with midi null but shift_midi set throws', () => {
	const map = mapModule.TRAKTOR_S4_MK3_MAP;
	const bad = {
		...layout.controls[0],
		id: 'bad-shift',
		midi: null,
		shift_midi: { ch: 1, kind: 'note', id: 0 }
	};
	assert.throws(() => bindings.resolveControl(map, bad), /no base wire/);
});
