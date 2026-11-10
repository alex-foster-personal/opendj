// requirement: SKIN-10858
// The DDJ-SB practice panel joins to DDJ_SB_MAP, the map transcribed from the official
// DDJ-SB List of MIDI messages.
//
//   if any panel control's wire identity is neither bound nor hinted by DDJ_SB_MAP then broken
//   if deck 1 PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if deck 2 pad 3 does not fire deck_hot_cue deck 2 slot C then broken
//   if BEAT SYNC resolves to anything but deck_sync_toggle then broken
//   if JOG or MASTER LEVEL resolve as bound while the map only hints them then broken
//   if a control with a wire number absent from the map resolves silently then broken
//   if TEMPO's value is not 14-bit (continuous14) then broken
//   if a documented shifted row does not resolve with shiftHeld then broken
//   if practiceMapFor does not return DDJ_SB_MAP for the layout then broken
//   if nameMatch matches DDJ-SB2 or DDJ-SB3 then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/ddj-sb/panel.json');

let vite;
let bindings;
let devices;
let registry;
let webmidi;
let DDJ_SB_MAP;
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
	({ DDJ_SB_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-sb.ts'));
});

after(async () => {
	await vite.close();
});

const byId = (id, shiftHeld = false) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return bindings.resolveControl(DDJ_SB_MAP, control, shiftHeld);
};

test('every DDJ-SB panel control resolves against DDJ_SB_MAP', () => {
	const resolved = bindings.resolveLayout(DDJ_SB_MAP, layout);
	assert.equal(resolved.length, 61);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	assert.ok(bound >= 35, `bound ${bound}`);
	for (const control of layout.controls) {
		if (control.shift_midi !== null) bindings.resolveControl(DDJ_SB_MAP, control, true);
	}
});

test('DDJ_SB_MAP is registered for hardware and for the practice route', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(DDJ_SB_MAP));
	assert.equal(devices.PRACTICE_DEVICE_MAPS.DDJ_SB_MAP, DDJ_SB_MAP);
	assert.equal(devices.practiceMapFor(layout), DDJ_SB_MAP);
});

test('nameMatch picks the SB, not the SB2 or SB3', () => {
	const re = new RegExp(DDJ_SB_MAP.nameMatch, 'i');
	assert.ok(re.test('DDJ-SB'));
	assert.ok(re.test('Pioneer DJ DDJ-SB MIDI 1'));
	assert.ok(!re.test('DDJ-SB2'));
	assert.ok(!re.test('DDJ-SB3'));
});

test('the map validates: no duplicate bindings, hints never shadow a binding', () => {
	webmidi.validateDeviceMap(DDJ_SB_MAP);
	const bound = new Set(
		DDJ_SB_MAP.bindings.map((b) => `${b.source.ch}:${b.source.kind}:${b.source.id}`)
	);
	const hinted = DDJ_SB_MAP.hints.map((h) => `${h.source.ch}:${h.source.kind}:${h.source.id}`);
	assert.equal(new Set(hinted).size, hinted.length, 'duplicate hint');
	for (const h of hinted) assert.ok(!bound.has(h), `hint ${h} collides with a binding`);
});

test('wire identity picks the same action as the controller', () => {
	assert.deepEqual(byId('d1-play-pause').binding.action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(byId('d2-pad-3').binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'C' });
	assert.deepEqual(byId('d1-sync').binding.action, { type: 'deck_sync_toggle', deck: 1 });
	assert.deepEqual(byId('ch1-filter').binding.action, {
		type: 'mixer_channel',
		deck: 1,
		target: 'filter'
	});
	assert.deepEqual(byId('load-2').binding.action, { type: 'browse_load', deck: 2 });
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.equal(byId('d1-tempo').binding.action.lsbOffset, 32);
	assert.equal(byId('phones-mix').binding.action.type, 'headphone_mix');
});

test('hinted-only controls are inert with the parity tooltip', () => {
	for (const id of [
		'd1-jog',
		'd1-vinyl',
		'd1-key-lock',
		'fx1-1',
		'master-level',
		'phones-vol',
		'filter-fade',
		'back'
	]) {
		const r = byId(id);
		assert.equal(r.kind, 'inert', id);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/);
	}
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(DDJ_SB_MAP, bogus), /neither binds nor hints/);
});

test('tempo sends a 14-bit value, knobs 7-bit', () => {
	assert.equal(
		bindings.inputValueFor(byId('d1-tempo').binding, { value01: 0.5 }).kind,
		'continuous14'
	);
	assert.equal(bindings.inputValueFor(byId('ch1-hi').binding, { value01: 1 }).raw, 127);
});

test('SHIFT layer: every documented shifted row resolves, bound or hinted', () => {
	const shifted = layout.controls.filter((c) => c.shift_midi !== null);
	assert.ok(shifted.length >= 20, `shifted rows ${shifted.length}`);
	for (const c of shifted) bindings.resolveControl(DDJ_SB_MAP, c, true);
	assert.equal(byId('d1-play-pause', true).kind, 'inert');
	assert.equal(byId('d1-play-pause', false).kind, 'bound');
	assert.equal(byId('browse', true).binding.source.id, 0x64);
	assert.equal(layout.controls.find((c) => c.id === 'd1-shift').shift_midi, null);
});
