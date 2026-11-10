// requirement: SKIN-12081 (Numark Mixstream Pro practice replica, issue #10870, ADR-0137)
//   if any panel control's wire is neither bound nor hinted by the map then broken
//   if the map binds anything before a hardware capture then broken
//   if any panel control resolves as bound then broken
//   if the map is missing from PRACTICE_DEVICE_MAPS or lands in DEVICE_MAP_REGISTRY with zero bindings then broken
//   if nameMatch misses Mixstream Pro or matches Mixstream Pro+, Go, or Mixtrack then broken
//   if validateDeviceMap throws or a hint source repeats then broken
//   if a wire number the map does not know resolves silently then broken

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
	'../../../tools/deck-diagrams/devices/numark-mixstream-pro/panel.json'
);

let vite;
let bindings;
let MAP;
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
	({ NUMARK_MIXSTREAM_PRO_MAP: MAP } = await vite.ssrLoadModule(
		'/src/lib/rb/midi/maps/numark-mixstream-pro.ts'
	));
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
	devices = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-devices.ts');
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
});

after(async () => {
	await vite.close();
});

const byId = (id, shift = false) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return bindings.resolveControl(MAP, control, shift);
};

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

const wiredCount = layout.controls.filter((c) => c.midi !== null).length;

test('every panel control resolves against the map on both layers, inert or unwired', () => {
	const resolved = bindings.resolveLayout(MAP, layout);
	assert.equal(resolved.length, 72);
	for (const r of resolved) {
		assert.ok(r.kind === 'inert' || r.kind === 'unwired', r.control.id);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/);
	}
});

test('the map binds nothing until a hardware capture verifies the community wires', () => {
	assert.deepEqual(MAP.bindings, []);
	assert.ok(MAP.hints.length >= wiredCount, `hints ${MAP.hints.length} wired ${wiredCount}`);
});

test('registered for the practice route only, never as a zero-binding hardware map', () => {
	assert.equal(devices.PRACTICE_DEVICE_MAPS.NUMARK_MIXSTREAM_PRO_MAP, MAP);
	assert.equal(devices.practiceMapFor(layout), MAP);
	assert.ok(!registry.DEVICE_MAP_REGISTRY.includes(MAP));
	for (const m of registry.DEVICE_MAP_REGISTRY) assert.ok(m.bindings.length > 0, m.nameMatch);
});

test('nameMatch picks Mixstream Pro and not its siblings', () => {
	const re = new RegExp(MAP.nameMatch, 'i');
	assert.ok(re.test('Mixstream Pro'));
	assert.ok(re.test('Numark Mixstream Pro MIDI 1'));
	assert.ok(!re.test('Mixstream Pro+'));
	assert.ok(!re.test('Mixstream Pro Go'));
	assert.ok(!re.test('Mixtrack Pro FX'));
});

test('the map validates and no hint source repeats', () => {
	webmidi.validateDeviceMap(MAP);
	const hinted = MAP.hints.map((h) => key(h.source));
	assert.equal(new Set(hinted).size, hinted.length, 'duplicate hint');
});

test('meters follow the community script on channel 16', () => {
	assert.equal(MAP.meters.length, 2);
	assert.deepEqual(MAP.meters[0].out, { ch: 16, cc: 32, maxValue: 127 });
	assert.deepEqual(MAP.meters[1].out, { ch: 16, cc: 33, maxValue: 127 });
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(MAP, bogus), /neither binds nor hints/);
});
