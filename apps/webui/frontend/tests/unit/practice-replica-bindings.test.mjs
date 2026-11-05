// requirement: SKIN-8871B
// The FLX4 practice panel joins to FLX4_MAP, the map the real controller drives.
//
//   if any panel control's wire identity is neither bound nor hinted by FLX4_MAP then broken
//   if deck 1 PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if deck 2 pad 3 does not fire deck_hot_cue deck 2 slot C then broken
//   if CFX on channel 2 does not fire mixer_channel filter deck 2 then broken
//   if JOG resolves as bound while the map only hints it then broken
//   if a control with a wire number absent from the map resolves silently then broken
//   if a bound control's hover title omits the action it fires then broken
//   if TEMPO's value is not 14-bit (continuous14) then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/ddj-flx4/panel.json');

let vite;
let bindings;
let FLX4_MAP;
const layout = JSON.parse(readFileSync(PANEL, 'utf8'));

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: { alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') }, conditions: ['browser'] }
	});
	bindings = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-bindings.ts');
	({ FLX4_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx4.ts'));
});

after(async () => {
	await vite.close();
});

const byId = (id) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return bindings.resolveControl(FLX4_MAP, control);
};

test('every FLX4 panel control resolves against FLX4_MAP', () => {
	const resolved = bindings.resolveLayout(FLX4_MAP, layout);
	assert.equal(resolved.length, 77);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	// Measured presence, not absence: most of the panel fires real actions.
	assert.ok(bound >= 45, `bound ${bound}`);
});

test('wire identity picks the same action as the controller', () => {
	assert.deepEqual(byId('d1-play-pause').binding.action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(byId('d2-pad-3').binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'C' });
	assert.deepEqual(byId('ch2-cfx').binding.action, {
		type: 'mixer_channel',
		deck: 2,
		target: 'filter'
	});
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
});

test('hinted-only controls are inert with the parity tooltip', () => {
	const jog = byId('d1-jog');
	assert.equal(jog.kind, 'inert');
	assert.match(bindings.controlTitle(jog), /not implemented - see PARITY-TODO/);
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(FLX4_MAP, bogus), /neither binds nor hints/);
});

test('hover title names the action fired', () => {
	assert.match(bindings.controlTitle(byId('d1-cue')), /Fires deck_cue \(deck 1\)/);
});

test('tempo sends a 14-bit value, knobs 7-bit', () => {
	const tempo = byId('d1-tempo');
	assert.equal(bindings.inputValueFor(tempo.binding, { value01: 0.5 }).kind, 'continuous14');
	assert.equal(bindings.inputValueFor(byId('ch1-trim').binding, { value01: 1 }).raw, 127);
});

// if the replica SHIFT reaches the action glue (which throws on shift_modifier) then broken
// if the replica SHIFT does not set and clear the modifier state then broken
test('SHIFT updates modifier state and never reaches the action glue', () => {
	const calls = { action: [], shift: [] };
	const sinks = {
		action: (action) => calls.action.push(action.type),
		shift: (held) => calls.shift.push(held)
	};
	const shift = byId('d1-shift');
	assert.equal(bindings.dispatchPractice(shift, { pressed: true }, sinks), 'modifier');
	assert.equal(bindings.dispatchPractice(shift, { pressed: false }, sinks), 'modifier');
	assert.deepEqual(calls.shift, [true, false]);
	assert.deepEqual(calls.action, []);
	// Control: a bound non-modifier still goes to the glue.
	assert.equal(
		bindings.dispatchPractice(byId('d1-play-pause'), { pressed: true }, sinks),
		'action'
	);
	assert.deepEqual(calls.action, ['deck_play_toggle']);
	assert.equal(bindings.dispatchPractice(byId('d1-jog'), { pressed: true }, sinks), 'inert');
});

// if holding SHIFT and pressing CH CUE fires the unshifted channel_cue then broken
// if SHIFT changes a control whose map has no shifted row then broken
test('SHIFT layer: a held SHIFT resolves the shifted wire, like the hardware', () => {
	const chCue = layout.controls.find((c) => c.id === 'ch1-ch-cue');
	assert.deepEqual(chCue.shift_midi, { ch: 1, kind: 'note', id: 0x68 });
	const base = bindings.resolveControl(FLX4_MAP, chCue, false);
	assert.deepEqual(base.binding.action, { type: 'channel_cue', deck: 1 });
	const shifted = bindings.resolveControl(FLX4_MAP, chCue, true);
	assert.equal(shifted.kind, 'inert', 'FLX4_MAP only hints shifted CH CUE');
	assert.match(shifted.hint, /CH CUE \+ SHIFT \(deck 1\)/);
	// A shifted row the map binds still fires: BROWSE + SHIFT is the same relative encoder.
	const browse = layout.controls.find((c) => c.id === 'browse');
	assert.equal(bindings.resolveControl(FLX4_MAP, browse, true).binding.source.id, 0x64);
	// Control: PLAY has no documented shifted row, so SHIFT leaves it alone.
	const play = layout.controls.find((c) => c.id === 'd1-play-pause');
	assert.equal(play.shift_midi, null);
	assert.deepEqual(bindings.resolveControl(FLX4_MAP, play, true).binding.action, {
		type: 'deck_play_toggle',
		deck: 1
	});
});
