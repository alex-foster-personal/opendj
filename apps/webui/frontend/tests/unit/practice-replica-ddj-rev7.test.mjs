// The DDJ-REV7 practice panel joins to REV7_MAP, the map the real controller drives
// (deck-replica checklist step 4; ADR-0137).
//
//   if any panel control's wire identity is neither bound nor hinted by REV7_MAP then broken
//   if deck 1 START/STOP does not fire deck_play_toggle deck 1 then broken
//   if deck 2 pad 3 does not fire deck_hot_cue deck 2 slot C then broken
//   if FILTER on channel 2 does not fire mixer_channel filter deck 2 then broken
//   if the JOG resolves inert while the map binds deck_jog then broken
//   if a hinted-only control (PITCH BEND) resolves as bound then broken
//   if the single SHIFT does not update modifier state without reaching the action glue then broken
//   if holding SHIFT on AUTO LOOP does not resolve its shifted row (note 0x50, hint) then broken
//   if TEMPO's value is not 14-bit (continuous14) then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/ddj-rev7/panel.json');

let vite;
let bindings;
let REV7_MAP;
let practiceMapFor;
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
	({ REV7_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-rev7.ts'));
	({ practiceMapFor } = await vite.ssrLoadModule(
		'/src/lib/components/rb/practice/practice-devices.ts'
	));
});

after(async () => {
	await vite.close();
});

const find = (id) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return control;
};
const byId = (id, shift = false) => bindings.resolveControl(REV7_MAP, find(id), shift);

test('the registry serves REV7_MAP for the panel and every control resolves', () => {
	assert.equal(practiceMapFor(layout), REV7_MAP);
	const resolved = bindings.resolveLayout(REV7_MAP, layout);
	assert.equal(resolved.length, layout.controls.length);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	assert.ok(bound >= 55, `bound ${bound} of ${resolved.length}`);
	const ids = layout.controls.map((c) => c.id);
	assert.equal(new Set(ids).size, ids.length, 'ids unique');
});

test('wire identity picks the same action as the controller', () => {
	assert.deepEqual(byId('d1-start-stop').binding.action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(byId('d2-pad-3').binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'C' });
	assert.deepEqual(byId('ch2-filter').binding.action, {
		type: 'mixer_channel',
		deck: 2,
		target: 'filter'
	});
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.deepEqual(byId('d1-jog').binding.action, { type: 'deck_jog', deck: 1 });
	assert.deepEqual(byId('d2-loop-half').binding.action, {
		type: 'deck_loop_scale',
		deck: 2,
		factor: 0.5
	});
});

test('hinted-only controls are inert with the parity tooltip', () => {
	const bend = byId('d1-pitch-bend-minus');
	assert.equal(bend.kind, 'inert');
	assert.match(bindings.controlTitle(bend), /not implemented - see PARITY-TODO/);
	assert.equal(byId('fx-tap').kind, 'inert');
	assert.equal(byId('d2-slot-1').kind, 'inert');
});

test('the single SHIFT updates modifier state and never reaches the action glue', () => {
	const calls = { action: [], shift: [] };
	const sinks = {
		action: (action) => calls.action.push(action.type),
		shift: (held) => calls.shift.push(held)
	};
	const shift = byId('shift');
	assert.deepEqual(shift.wire, { ch: 7, kind: 'note', id: 0x3f });
	assert.equal(bindings.dispatchPractice(shift, { pressed: true }, sinks), 'modifier');
	assert.equal(bindings.dispatchPractice(shift, { pressed: false }, sinks), 'modifier');
	assert.deepEqual(calls.shift, [true, false]);
	assert.deepEqual(calls.action, []);
	assert.equal(
		bindings.dispatchPractice(byId('d1-start-stop'), { pressed: true }, sinks),
		'action'
	);
	assert.deepEqual(calls.action, ['deck_play_toggle']);
});

test('SHIFT layer: a held SHIFT resolves the shifted wire, like the hardware', () => {
	const loop = find('d1-auto-loop');
	assert.deepEqual(loop.shift_midi, { ch: 1, kind: 'note', id: 0x50 });
	assert.deepEqual(byId('d1-auto-loop').binding.action, { type: 'deck_auto_loop_toggle', deck: 1 });
	const shifted = byId('d1-auto-loop', true);
	assert.equal(shifted.kind, 'inert');
	assert.match(shifted.hint, /AUTO LOOP \+ SHIFT \(deck 1\)/);
	// BROWSE + SHIFT is the same relative encoder on CC 0x64, bound.
	assert.equal(byId('d1-browse', true).binding.source.id, 0x64);
	// Control: the pads' HOT CUE MODE + SHIFT row is a hint, so SHIFT makes a pad inert.
	assert.equal(byId('d2-pad-8', true).kind, 'inert');
	// Control: a knob with no shifted row is left alone.
	assert.equal(find('ch1-trim').shift_midi, null);
});

test('tempo sends a 14-bit value, knobs 7-bit', () => {
	const tempo = byId('d2-tempo');
	assert.equal(bindings.inputValueFor(tempo.binding, { value01: 0.5 }).kind, 'continuous14');
	assert.equal(bindings.inputValueFor(byId('ch1-trim').binding, { value01: 1 }).raw, 127);
});
