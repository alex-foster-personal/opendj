// requirement: SKIN-10480, SKIN-8871B (DDJ-SX3 replica, epic #9366)
// The DDJ-SX3 practice panel joins to SX3_MAP, the map the real controller drives.
//
//   if any panel control's wire identity is neither bound nor hinted by SX3_MAP then broken
//   if deck 2 PLAY/PAUSE does not fire deck_play_toggle deck 2 then broken
//   if deck 2 pad 3 does not fire deck_hot_cue deck 2 slot C on pad channel 9 then broken
//   if COLOR on channel 4 does not fire mixer_channel filter deck 4 then broken
//   if LOAD 3 does not fire browse_load deck 3 then broken
//   if the FX knobs or NEEDLE SEARCH resolve as bound while the map only hints them then broken
//   if practiceMapFor cannot find SX3_MAP by the panel's device_map name then broken
//   if TEMPO's value is not 14-bit (continuous14) then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/ddj-sx3/panel.json');

let vite;
let bindings;
let devices;
let SX3_MAP;
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
	({ SX3_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-sx3.ts'));
});

after(async () => {
	await vite.close();
});

const byId = (id, shift = false) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return bindings.resolveControl(SX3_MAP, control, shift);
};

test('every SX3 panel control resolves against SX3_MAP, on both layers', () => {
	const resolved = bindings.resolveLayout(SX3_MAP, layout);
	assert.equal(resolved.length, layout.controls.length);
	assert.ok(layout.controls.length >= 130, `controls ${layout.controls.length}`);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	// Measured presence, not absence: the transport, mixer, pads and loop rows fire real actions.
	assert.ok(bound >= 70, `bound ${bound}`);
	// The SHIFT layer resolves too (a shifted wire the map knows nothing about would throw).
	for (const c of layout.controls) bindings.resolveControl(SX3_MAP, c, true);
});

test('the registry serves SX3_MAP under the panel device_map name', () => {
	assert.equal(layout.device_map, 'SX3_MAP');
	assert.equal(devices.practiceMapFor(layout), SX3_MAP);
	assert.equal(SX3_MAP.nameMatch, 'DDJ-SX3');
});

test('wire identity picks the same action as the controller', () => {
	assert.deepEqual(byId('d2-play-pause').binding.action, { type: 'deck_play_toggle', deck: 2 });
	const pad = byId('d2-pad-3');
	assert.deepEqual(pad.wire, { ch: 9, kind: 'note', id: 2 });
	assert.deepEqual(pad.binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'C' });
	assert.deepEqual(byId('ch4-color').binding.action, {
		type: 'mixer_channel',
		deck: 4,
		target: 'filter'
	});
	assert.deepEqual(byId('load-3').binding.action, { type: 'browse_load', deck: 3 });
	assert.deepEqual(byId('d1-auto-loop').binding.action, { type: 'deck_auto_loop_toggle', deck: 1 });
	assert.deepEqual(byId('d1-loop-half').binding.action, {
		type: 'deck_loop_scale',
		deck: 1,
		factor: 0.5
	});
	assert.deepEqual(byId('d1-key-up').binding.action, {
		type: 'deck_key_nudge',
		deck: 1,
		semitones: 1
	});
	assert.deepEqual(byId('d1-key-up', true).binding.action, {
		type: 'deck_key_sync_toggle',
		deck: 1
	});
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.equal(byId('d1-deck-3').kind, 'inert');
});

test('hinted-only controls are inert with the parity tooltip', () => {
	for (const id of [
		'fx1-knob-1',
		'fx2-beats',
		'd1-needle-search',
		'd2-censor',
		'echo',
		'sampler-volume',
		'usb-a'
	]) {
		const r = byId(id);
		assert.equal(r.kind, 'inert', id);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/);
	}
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(SX3_MAP, bogus), /neither binds nor hints/);
});

test('tempo sends a 14-bit value, knobs 7-bit', () => {
	const tempo = byId('d2-tempo');
	assert.equal(bindings.inputValueFor(tempo.binding, { value01: 0.5 }).kind, 'continuous14');
	assert.equal(bindings.inputValueFor(byId('ch3-trim').binding, { value01: 1 }).raw, 127);
});

test('LED rules cover every bound button the MIDI list documents as MIDI-OUT', () => {
	const outs = new Set(SX3_MAP.leds.map((r) => `${r.out.ch}:${r.out.note}`));
	for (const deck of [1, 2, 3, 4]) {
		for (const note of [0x0b, 0x0c, 0x5d, 0x14, 0x54, 0x1b, 0x22])
			assert.ok(outs.has(`${deck}:${note}`), `deck ${deck} note ${note}`);
		for (let pad = 0; pad < 8; pad++)
			assert.ok(outs.has(`${7 + deck}:${pad}`), `deck ${deck} pad ${pad + 1}`);
	}
});
