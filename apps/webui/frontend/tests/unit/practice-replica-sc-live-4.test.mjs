// requirement: SKIN-SC-LIVE-4 (Denon DJ SC LIVE 4 practice replica, epic #9366)
// The SC LIVE 4 panel joins to SC_LIVE_4_MAP, a hints-only map: no MIDI message list is
// published for this unit, so every control is inert with the PARITY-TODO tooltip.
//
//   if any panel control's wire identity is neither bound nor hinted by SC_LIVE_4_MAP then broken
//   if any control resolves as bound (the map must bind nothing) then broken
//   if SC_LIVE_4_MAP is missing from PRACTICE_DEVICE_MAPS or present in DEVICE_MAP_REGISTRY then broken
//   if a hint leaves the placeholder channel, repeats, or a hint label omits the control id then broken
//   if validateDeviceMap(SC_LIVE_4_MAP) throws then broken
//   if an inert control's hover title omits the PARITY-TODO pointer then broken
//   if a wire number the map does not know resolves silently then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/sc-live-4/panel.json');
const CONTROL_COUNT = 128;

let vite;
let bindings;
let SC_LIVE_4_MAP;
let SC_LIVE_4_PLACEHOLDER_CH;
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
	({ SC_LIVE_4_MAP, SC_LIVE_4_PLACEHOLDER_CH } = await vite.ssrLoadModule(
		'/src/lib/rb/midi/maps/sc-live-4.ts'
	));
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
	devices = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-devices.ts');
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

test('every SC LIVE 4 panel control resolves inert against SC_LIVE_4_MAP', () => {
	const resolved = bindings.resolveLayout(SC_LIVE_4_MAP, layout);
	assert.equal(resolved.length, CONTROL_COUNT);
	assert.equal(resolved.filter((r) => r.kind === 'bound').length, 0, 'nothing may bind');
	for (const r of resolved) {
		assert.equal(r.kind, 'inert', r.control.id);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/);
		assert.ok(r.hint.includes(`[${r.control.id}]`), `hint names ${r.control.id}`);
	}
	for (const control of layout.controls) assert.equal(control.shift_midi, null, control.id);
});

test('SC_LIVE_4_MAP is a practice map only: never offered to hardware', () => {
	assert.equal(SC_LIVE_4_MAP.bindings.length, 0);
	assert.equal(devices.PRACTICE_DEVICE_MAPS.SC_LIVE_4_MAP, SC_LIVE_4_MAP);
	assert.equal(devices.practiceMapFor(layout), SC_LIVE_4_MAP);
	assert.ok(
		!registry.DEVICE_MAP_REGISTRY.includes(SC_LIVE_4_MAP),
		'placeholder wires must never match a real port'
	);
});

test('the map validates and every hint is a unique placeholder on channel 16', () => {
	webmidi.validateDeviceMap(SC_LIVE_4_MAP);
	assert.equal(SC_LIVE_4_PLACEHOLDER_CH, 16);
	const hinted = SC_LIVE_4_MAP.hints.map((h) => key(h.source));
	assert.equal(hinted.length, CONTROL_COUNT);
	assert.equal(new Set(hinted).size, hinted.length, 'duplicate hint');
	for (const h of SC_LIVE_4_MAP.hints) {
		assert.equal(h.source.ch, SC_LIVE_4_PLACEHOLDER_CH);
		assert.match(h.label, /wire unpublished$/);
	}
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x0b } };
	assert.throws(() => bindings.resolveControl(SC_LIVE_4_MAP, bogus), /neither binds nor hints/);
});
