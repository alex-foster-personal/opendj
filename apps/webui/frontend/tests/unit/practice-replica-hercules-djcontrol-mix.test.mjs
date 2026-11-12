// requirement: SKIN-10862 (Hercules DJControl MIX practice replica, issue #10862)
// The MIX practice panel joins to HERCULES_DJCONTROL_MIX_MAP, a hints-only map (no vendor MIDI list).
//
//   if any panel control's wire identity (base or SHIFT) is neither bound nor hinted by the map then broken
//   if the map binds anything before a hardware capture verifies the community wires then broken
//   if any panel control resolves as bound (every control must be inert with the PARITY-TODO title) then broken
//   if the map is missing from PRACTICE_DEVICE_MAPS, or sneaks into DEVICE_MAP_REGISTRY with zero bindings then broken
//   if nameMatch misses "DJControl MIX" or matches MIX Ultra, Inpulse 200 or RMX2 then broken
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
	'../../../tools/deck-diagrams/devices/hercules-djcontrol-mix/panel.json'
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
	({ HERCULES_DJCONTROL_MIX_MAP: MAP } = await vite.ssrLoadModule(
		'/src/lib/rb/midi/maps/hercules-djcontrol-mix.ts'
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

test('every panel control resolves against the map on both layers, all of them inert', () => {
	const resolved = bindings.resolveLayout(MAP, layout);
	assert.equal(resolved.length, 31);
	for (const r of resolved) {
		assert.equal(r.kind, 'inert', r.control.id);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/);
	}
	for (const control of layout.controls) {
		if (control.shift_midi !== null) {
			assert.equal(bindings.resolveControl(MAP, control, true).kind, 'inert', control.id);
		}
	}
});

test('the map binds nothing until a hardware capture verifies the community wires', () => {
	assert.deepEqual(MAP.bindings, []);
	assert.ok(MAP.hints.length >= 31, `hints ${MAP.hints.length}`);
});

test('registered for the practice route only, never as a zero-binding hardware map', () => {
	assert.equal(devices.PRACTICE_DEVICE_MAPS.HERCULES_DJCONTROL_MIX_MAP, MAP);
	assert.equal(devices.practiceMapFor(layout), MAP);
	assert.ok(!registry.DEVICE_MAP_REGISTRY.includes(MAP));
	for (const m of registry.DEVICE_MAP_REGISTRY) assert.ok(m.bindings.length > 0, m.nameMatch);
});

test('nameMatch picks the DJControl MIX port and not its siblings', () => {
	const re = new RegExp(MAP.nameMatch, 'i');
	assert.ok(re.test('DJControl MIX'));
	assert.ok(re.test('Hercules DJControl MIX MIDI 1'));
	assert.ok(!re.test('DJControl MIX Ultra'));
	assert.ok(!re.test('DJControl Inpulse 200'));
	assert.ok(!re.test('DJConsole RMX2'));
});

test('the map validates and no hint source repeats', () => {
	webmidi.validateDeviceMap(MAP);
	const hinted = MAP.hints.map((h) => key(h.source));
	assert.equal(new Set(hinted).size, hinted.length, 'duplicate hint');
});

test('hints name the control: SHIFT layer resolves the shifted wire like the hardware', () => {
	assert.match(byId('d1-play').hint, /PLAY\/PAUSE \(deck 1\)/);
	assert.match(byId('d1-play', true).hint, /PLAY \+ SHIFT \(stutter\)/);
	assert.match(byId('d2-pad-3').hint, /PAD 3 hot cue \(deck 2\)/);
	assert.match(byId('d2-pad-3', true).hint, /PAD 3 \+ SHIFT \(clear\) \(deck 2\)/);
	assert.match(byId('ch2-filter').hint, /FILTER \(channel 2\)/);
	assert.deepEqual(byId('crossfader').wire, { ch: 1, kind: 'cc', id: 0 });
});

test('no LED or meter rules without verified output identity', () => {
	assert.ok(!MAP.leds || MAP.leds.length === 0);
	assert.ok(!MAP.meters || MAP.meters.length === 0);
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(MAP, bogus), /neither binds nor hints/);
});
