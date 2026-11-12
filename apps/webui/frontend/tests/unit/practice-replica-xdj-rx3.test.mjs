// requirement: SKIN-10482, SKIN-8871B, SKIN-9407
// The XDJ-RX3 practice panel joins to XDJ_RX3_MAP, the map the real unit drives.
//
//   if any panel control's wire identity is neither bound nor hinted by XDJ_RX3_MAP then broken
//   if practiceMapFor does not resolve the panel's device_map to XDJ_RX3_MAP then broken
//   if deck 1 PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if deck 2 pad 3 does not fire deck_hot_cue deck 2 slot C then broken
//   if the COLOR knob on channel 2 does not fire mixer_channel filter deck 2 then broken
//   if the jog platter does not fire deck_jog as a centre-64 relative wire then broken
//   if a hint-only control (QUANTIZE, TAP) resolves as bound then broken
//   if a held SHIFT on the jog does not resolve the list's shifted platter row then broken
//   if a control with a wire number absent from the map resolves silently then broken
//   if TEMPO's value is not 14-bit (continuous14) then broken
//   if the RX3 LED rules do not light CUE on deck_loaded and HOT CUE mode on pad_mode_selected then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/xdj-rx3/panel.json');

let vite;
let bindings;
let devices;
let XDJ_RX3_MAP;
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
	({ XDJ_RX3_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/pioneer-xdj.ts'));
});

after(async () => {
	await vite.close();
});

const control = (id) => {
	const c = layout.controls.find((x) => x.id === id);
	assert.ok(c, `panel has ${id}`);
	return c;
};
const byId = (id, shift = false) => bindings.resolveControl(XDJ_RX3_MAP, control(id), shift);

test('the registry resolves the panel to XDJ_RX3_MAP and every control resolves', () => {
	assert.equal(devices.practiceMapFor(layout), XDJ_RX3_MAP);
	const resolved = bindings.resolveLayout(XDJ_RX3_MAP, layout);
	assert.equal(resolved.length, 119);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	// Measured presence: transport, loops, pads, jogs, mixer and browse are real actions.
	assert.ok(bound >= 50, `bound ${bound}`);
});

test('wire identity picks the same action as the unit', () => {
	assert.deepEqual(byId('d1-play-pause').binding.action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(byId('d2-pad-3').binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'C' });
	assert.deepEqual(byId('ch2-color').binding.action, {
		type: 'mixer_channel',
		deck: 2,
		target: 'filter'
	});
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.deepEqual(byId('d2-load').binding.action, { type: 'browse_load', deck: 2 });
	assert.equal(byId('ch1-ch-cue').binding.ccButton, true);
});

test('the jog platter is a bound centre-64 relative wire on this unit', () => {
	const jog = byId('d1-jog');
	assert.equal(jog.kind, 'bound');
	assert.deepEqual(jog.binding.action, { type: 'deck_jog', deck: 1 });
	assert.equal(jog.binding.relativeCentered, true);
	assert.deepEqual(control('d1-jog').shift_midi, { ch: 1, kind: 'cc', id: 41 });
	const shifted = byId('d1-jog', true);
	assert.equal(shifted.kind, 'inert');
	assert.match(shifted.hint, /JOG platter \+ SHIFT \(deck 1\)/);
});

test('hint-only controls are inert with the parity tooltip', () => {
	for (const id of [
		'd1-quantize',
		'tap',
		'effect-select',
		'd2-mode-beat-jump',
		'ch1-input-switch'
	]) {
		const r = byId(id);
		assert.equal(r.kind, 'inert', id);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/);
	}
	// Control: PLAY has no documented shifted row, so SHIFT leaves it alone.
	assert.equal(control('d1-play-pause').shift_midi, null);
	assert.deepEqual(byId('d1-play-pause', true).binding.action, {
		type: 'deck_play_toggle',
		deck: 1
	});
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(XDJ_RX3_MAP, bogus), /neither binds nor hints/);
});

test('tempo sends a 14-bit value, knobs 7-bit', () => {
	assert.equal(
		bindings.inputValueFor(byId('d1-tempo').binding, { value01: 0.5 }).kind,
		'continuous14'
	);
	assert.equal(bindings.inputValueFor(byId('ch1-trim').binding, { value01: 1 }).raw, 127);
});

test('RX3 LED rules cover the CUE and HOT CUE mode OUT rows for both decks', () => {
	for (const deck of [1, 2]) {
		const cue = XDJ_RX3_MAP.leds.find(
			(r) => r.trigger.kind === 'deck_loaded' && r.trigger.deck === deck
		);
		assert.deepEqual(cue.out, { ch: deck, note: 1, velocityOn: 0x7f, velocityOff: 0x00 });
		const mode = XDJ_RX3_MAP.leds.find(
			(r) => r.trigger.kind === 'pad_mode_selected' && r.trigger.deck === deck
		);
		assert.equal(mode.trigger.mode, 'hot_cue');
		assert.deepEqual(mode.out, { ch: deck, note: 34, velocityOn: 0x7f, velocityOff: 0x00 });
	}
	// Hints never duplicate a binding and never repeat a source.
	const key = (s) => `${s.ch}:${s.kind}:${s.id}`;
	const bound = new Set(XDJ_RX3_MAP.bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of XDJ_RX3_MAP.hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint ${h.label} duplicates a binding`);
		assert.ok(!seen.has(k), `hint ${h.label} repeats ${k}`);
		seen.add(k);
	}
});
