// requirement: SKIN-10491 (Numark Mixtrack Pro FX practice replica, epic #9366)
// The Mixtrack Pro FX practice panel joins to NUMARK_MIXTRACK_PRO_FX_MAP, a hints-only map
// (Numark publishes no MIDI list; the wires are the community Mixxx mapping, unverified).
//
//   if any panel control's wire identity (base or SHIFT) is neither bound nor hinted by the map then broken
//   if the map binds anything before a hardware capture verifies the community wires then broken
//   if any panel control resolves as bound (every control must be inert with the PARITY-TODO title) then broken
//   if the map is missing from PRACTICE_DEVICE_MAPS, or sneaks into DEVICE_MAP_REGISTRY with zero bindings then broken
//   if nameMatch misses "MixTrack Pro FX" or matches Mixtrack Pro, Mixtrack Pro 3 or Mixtrack Platinum FX then broken
//   if validateDeviceMap throws or a hint source repeats then broken
//   if the PLAY LED rule is not deck_playing on channel 1 note 0, or the meters are not CC 31 on channels 1 and 2 then broken
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
	'../../../tools/deck-diagrams/devices/numark-mixtrack-pro-fx/panel.json'
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
	({ NUMARK_MIXTRACK_PRO_FX_MAP: MAP } = await vite.ssrLoadModule(
		'/src/lib/rb/midi/maps/numark-mixtrack-pro-fx.ts'
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
	assert.equal(resolved.length, 80);
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
	assert.ok(MAP.hints.length >= 80, `hints ${MAP.hints.length}`);
});

test('registered for the practice route only, never as a zero-binding hardware map', () => {
	assert.equal(devices.PRACTICE_DEVICE_MAPS.NUMARK_MIXTRACK_PRO_FX_MAP, MAP);
	assert.equal(devices.practiceMapFor(layout), MAP);
	assert.ok(!registry.DEVICE_MAP_REGISTRY.includes(MAP));
	for (const m of registry.DEVICE_MAP_REGISTRY) assert.ok(m.bindings.length > 0, m.nameMatch);
});

test('nameMatch picks the Mixtrack Pro FX port and not its siblings', () => {
	const re = new RegExp(MAP.nameMatch, 'i');
	assert.ok(re.test('MixTrack Pro FX'));
	assert.ok(re.test('Numark MixTrack Pro FX MIDI 1'));
	assert.ok(!re.test('Mixtrack Pro'));
	assert.ok(!re.test('Mixtrack Pro 3'));
	assert.ok(!re.test('Mixtrack Pro II'));
	assert.ok(!re.test('Mixtrack Platinum FX'));
});

test('the map validates and no hint source repeats', () => {
	webmidi.validateDeviceMap(MAP);
	const hinted = MAP.hints.map((h) => key(h.source));
	assert.equal(new Set(hinted).size, hinted.length, 'duplicate hint');
});

test('hints name the control: SHIFT layer resolves the shifted wire like the hardware', () => {
	assert.match(byId('d1-play').hint, /PLAY\/PAUSE \(deck 1\)/);
	assert.match(byId('d1-play', true).hint, /PLAY\/PAUSE \+ SHIFT \(deck 1\)/);
	assert.match(byId('d2-pad-3').hint, /PAD 3 \(deck 2\)/);
	assert.match(byId('d2-pad-3', true).hint, /PAD 3 \+ SHIFT \(deck 2\)/);
	assert.match(byId('ch2-filter').hint, /FILTER \(channel 2\)/);
	assert.match(byId('fx-reverb').hint, /REVERB/);
	assert.match(byId('browse', true).hint, /BROWSE turn \+ SHIFT/);
	assert.deepEqual(byId('crossfader').wire, { ch: 16, kind: 'cc', id: 8 });
});

test('LED and meter rules follow the community script: same notes as the inputs, CC 31 meters', () => {
	const play = MAP.leds.find((r) => r.trigger.kind === 'deck_playing' && r.trigger.deck === 1);
	assert.deepEqual(play.out, { ch: 1, note: 0, velocityOn: 0x7f, velocityOff: 0x01 });
	assert.ok(
		MAP.leds.some((r) => r.trigger.kind === 'loop_engaged' && r.out.ch === 6 && r.out.note === 64)
	);
	assert.ok(
		MAP.leds.some(
			(r) => r.trigger.kind === 'hot_cue_present' && r.trigger.slot === 'D' && r.out.note === 23
		)
	);
	assert.equal(MAP.meters.length, 2);
	assert.deepEqual(MAP.meters[1].out, { ch: 2, cc: 0x1f, maxValue: 90 });
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(MAP, bogus), /neither binds nor hints/);
});
