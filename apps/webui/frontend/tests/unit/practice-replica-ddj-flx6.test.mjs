// requirement: SKIN-FLX6 (DDJ-FLX6 practice replica, epic #9366)
// The FLX6 practice panel joins to FLX6_MAP, the map the real controller drives.
//
//   if any panel control's wire identity (base or SHIFT) is neither bound nor hinted by FLX6_MAP then broken
//   if FLX6_MAP is missing from DEVICE_MAP_REGISTRY or PRACTICE_DEVICE_MAPS then broken
//   if nameMatch misses DDJ-FLX6 / DDJ-FLX6-GT or matches DDJ-FLX4 / DDJ-FLX10 then broken
//   if registerDeviceMap(FLX6_MAP) throws (duplicate or invalid bindings) then broken
//   if deck 1 PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if channel 3 FILTER does not fire mixer_channel filter deck 3 on ch 7 CC 25 then broken
//   if LOAD 3 does not fire browse_load deck 3 then broken
//   if BEAT SYNC does not fire deck_sync_toggle then broken
//   if JOG resolves as bound while the map only hints it then broken
//   if a hint collides with a bound source then broken
//   if an LED rule exists for a row the sheet prints no MIDI-OUT for (RELOOP/EXIT) then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/ddj-flx6/panel.json');

let vite;
let bindings;
let FLX6_MAP;
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
	({ FLX6_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx6.ts'));
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
	return bindings.resolveControl(FLX6_MAP, control, shift);
};

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

test('every FLX6 panel control resolves against FLX6_MAP on both layers', () => {
	const resolved = bindings.resolveLayout(FLX6_MAP, layout);
	assert.equal(resolved.length, 104);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	assert.ok(bound >= 60, `bound ${bound}`);
	for (const control of layout.controls) {
		if (control.shift_midi !== null) bindings.resolveControl(FLX6_MAP, control, true);
	}
});

test('FLX6_MAP is registered for hardware and for the practice route', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(FLX6_MAP));
	assert.equal(devices.PRACTICE_DEVICE_MAPS.FLX6_MAP, FLX6_MAP);
	assert.equal(devices.practiceMapFor(layout), FLX6_MAP);
});

test('nameMatch picks the FLX6 and the FLX6-GT, not the FLX4 or FLX10', () => {
	const re = new RegExp(FLX6_MAP.nameMatch, 'i');
	assert.ok(re.test('DDJ-FLX6'));
	assert.ok(re.test('Pioneer DJ DDJ-FLX6-GT MIDI 1'));
	assert.ok(!re.test('DDJ-FLX4'));
	assert.ok(!re.test('DDJ-FLX10'));
});

test('the map validates: no duplicate bindings, hints never shadow a binding', () => {
	webmidi.validateDeviceMap(FLX6_MAP);
	const bound = new Set(FLX6_MAP.bindings.map((b) => key(b.source)));
	const hinted = FLX6_MAP.hints.map((h) => key(h.source));
	assert.equal(new Set(hinted).size, hinted.length, 'duplicate hint');
	for (const h of hinted) assert.ok(!bound.has(h), `hint ${h} collides with a binding`);
});

test('wire identity picks the same action as the controller', () => {
	assert.deepEqual(byId('d1-play-pause').binding.action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(byId('d1-jog').binding.action, { type: 'deck_jog', deck: 1 });
	assert.deepEqual(byId('d2-pad-3').binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'C' });
	const filter = byId('ch3-filter');
	assert.deepEqual(filter.binding.source, { ch: 7, kind: 'cc', id: 0x19 });
	assert.deepEqual(filter.binding.action, { type: 'mixer_channel', deck: 3, target: 'filter' });
	assert.deepEqual(byId('load-3').binding.action, { type: 'browse_load', deck: 3 });
	assert.deepEqual(byId('d1-beat-sync').binding.action, { type: 'deck_sync_toggle', deck: 1 });
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.equal(byId('d1-tempo').binding.action.lsbOffset, 32);
});

test('hinted-only controls are inert with the parity tooltip', () => {
	for (const id of ['d1-merge-fx', 'd2-deck', 'fx-on', 'input-select', 'booth']) {
		const r = byId(id);
		assert.equal(r.kind, 'inert', id);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/);
	}
});

test('SHIFT layer: a held SHIFT resolves the shifted wire, like the hardware', () => {
	const chCue = byId('ch1-ch-cue', true);
	assert.equal(chCue.kind, 'inert');
	assert.match(chCue.hint, /CH CUE \+ SHIFT \(deck 1\)/);
	assert.equal(byId('browse', true).binding.source.id, 0x64);
	// IN/-4BEAT press is hinted, its long-press 4-beat loop row is the binding on note 0x14.
	assert.equal(byId('d1-in').kind, 'inert');
	assert.ok(FLX6_MAP.bindings.some((b) => key(b.source) === '1:note:20' && b.action.beats === 4));
});

test('LED rules follow the MIDI-OUT column: IN/OUT light a loop, RELOOP/EXIT does not', () => {
	const outs = FLX6_MAP.leds.filter((r) => r.out.ch === 1).map((r) => r.out.note);
	assert.ok(outs.includes(0x10) && outs.includes(0x11));
	assert.ok(!outs.includes(0x4d));
	assert.ok(
		FLX6_MAP.leds.some((r) => r.trigger.kind === 'beat_sync_enabled' && r.out.note === 0x58)
	);
	assert.equal(FLX6_MAP.meters.length, 4);
	assert.deepEqual(FLX6_MAP.meters[2].out, { ch: 3, cc: 0x02, maxValue: 0x7f });
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(FLX6_MAP, bogus), /neither binds nor hints/);
});
