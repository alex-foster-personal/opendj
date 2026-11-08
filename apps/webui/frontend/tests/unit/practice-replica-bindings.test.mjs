// requirement: SKIN-8871B, SKIN-9564
// The FLX4 practice panel joins to FLX4_MAP, the map the real controller drives.
//
//   if any panel control's wire identity is neither bound nor hinted by FLX4_MAP then broken
//   if deck 1 PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if deck 2 pad 3 does not fire deck_hot_cue deck 2 slot C then broken
//   if CFX on channel 2 does not fire mixer_channel filter deck 2 then broken
//   if JOG or any SETTLED_INERT control resolves as bound then broken
//   if the bound count is not exactly 56 then broken
//   if PAD FX or SAMPLER resolves as bound while their pad modes are unavailable then broken
//   if any still-inert control has an empty reason then broken
//   if a newly bound control does not dispatch its MidiAction then broken
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

/** Expected MidiAction for each control the #9405 supplement binds (literal copy; do not import PRACTICE_WIRE_BINDINGS). */
const NEWLY_BOUND = {
	'd1-beat-sync': { type: 'deck_sync_toggle', deck: 1 },
	'd2-beat-sync': { type: 'deck_sync_toggle', deck: 2 },
	'd1-in': { type: 'deck_manual_loop_cycle', deck: 1 },
	'd2-in': { type: 'deck_manual_loop_cycle', deck: 2 },
	'd1-out': { type: 'deck_manual_loop_cycle', deck: 1 },
	'd2-out': { type: 'deck_manual_loop_cycle', deck: 2 },
	'd1-hot-cue': { type: 'controller_pad_mode', deck: 1, mode: 'hot_cue' },
	'd2-hot-cue': { type: 'controller_pad_mode', deck: 2, mode: 'hot_cue' }
};

/** Remaining inert controls after the supplement; one non-empty reason each (condensed from issue #9405). */
const SETTLED_INERT = {
	'd1-jog':
		'deck_jog needs relative motion but the replica jog is a press button, so dispatch would throw; drag jog is out of scope here.',
	'd2-jog':
		'deck_jog needs relative motion but the replica jog is a press button, so dispatch would throw; drag jog is out of scope here.',
	'd1-beat-jump':
		'ControllerPadMode has no beat_jump; binding another mode would mislabel the control.',
	'd2-beat-jump':
		'ControllerPadMode has no beat_jump; binding another mode would mislabel the control.',
	'd1-call-prev': 'The MidiAction union has no cue or loop-call action.',
	'd1-call-next': 'The MidiAction union has no cue or loop-call action.',
	'd2-call-prev': 'The MidiAction union has no cue or loop-call action.',
	'd2-call-next': 'The MidiAction union has no cue or loop-call action.',
	'mic-level': 'The app has no microphone channel or mic MIDI action.',
	'smart-cfx': 'The MidiAction union has no Smart CFX action.',
	'smart-fader': 'The MidiAction union has no Smart Fader action.',
	'fx-ch': 'Beat FX are outside the MIDI contract; no fx_* action exists.',
	'fx-select': 'Beat FX are outside the MIDI contract; no fx_* action exists.',
	'beat-prev': 'Beat FX are outside the MIDI contract; no fx_* action exists.',
	'beat-next': 'Beat FX are outside the MIDI contract; no fx_* action exists.',
	'level-depth': 'Beat FX are outside the MIDI contract; no fx_* action exists.',
	'fx-on': 'Beat FX are outside the MIDI contract; no fx_* action exists.',
	'd1-pad-fx':
		'instant_fx pad mode is unavailable (selectControllerPadMode warns) and replica pads fire deck_hot_cue regardless of mode, so binding it would fake PAD FX.',
	'd2-pad-fx':
		'instant_fx pad mode is unavailable (selectControllerPadMode warns) and replica pads fire deck_hot_cue regardless of mode, so binding it would fake PAD FX.',
	'd1-sampler':
		'sampler pad mode is unavailable (selectControllerPadMode warns) and replica pads fire deck_hot_cue regardless of mode, so binding it would fake SAMPLER.',
	'd2-sampler':
		'sampler pad mode is unavailable (selectControllerPadMode warns) and replica pads fire deck_hot_cue regardless of mode, so binding it would fake SAMPLER.'
};

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
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: { alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') }, conditions: ['browser'] }
	});
	bindings = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-bindings.ts');
	({ FLX4_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx4.ts'));
});

after(async () => {
	await vite.close();
});

const controlById = (id) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return control;
};

const byId = (id) => bindings.resolveControl(FLX4_MAP, controlById(id));

test('every FLX4 panel control resolves against FLX4_MAP', () => {
	const resolved = bindings.resolveLayout(FLX4_MAP, layout);
	assert.equal(resolved.length, 77);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	// 48 before #9405, +8 supplement
	assert.equal(bound, 56, `bound ${bound}`);
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

test('still-inert controls are exactly SETTLED_INERT', () => {
	const resolved = bindings.resolveLayout(FLX4_MAP, layout);
	const inertIds = resolved
		.filter((r) => r.kind === 'inert')
		.map((r) => r.control.id)
		.sort();
	const expected = Object.keys(SETTLED_INERT).sort();
	assert.deepEqual(inertIds, expected);
	for (const id of expected) {
		const r = resolved.find((x) => x.control.id === id);
		assert.ok(r && r.kind === 'inert', `${id} should be inert`);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/, id);
	}
	for (const id of Object.keys(NEWLY_BOUND)) {
		assert.equal(byId(id).kind, 'bound', `${id} should be supplement-bound`);
	}
});

test('every still-inert control carries a non-empty reason', () => {
	const entries = Object.entries(SETTLED_INERT);
	assert.equal(entries.length, 21, 'SETTLED_INERT count');
	for (const [id, reason] of entries) {
		assert.equal(typeof reason, 'string', id);
		assert.ok(reason.trim().length > 0, `${id} has no reason`);
	}
});

test('each newly bound control dispatches its MidiAction', () => {
	const expectedVelocity = { kind: 'button', pressed: true, velocity: 127 };
	for (const [id, expected] of Object.entries(NEWLY_BOUND)) {
		const r = byId(id);
		assert.equal(r.kind, 'bound', `${id}: resolve`);
		assert.deepEqual(r.binding.action, expected, `${id}: action`);
		assert.match(bindings.controlTitle(r), /Fires /, `${id}: title`);
		const calls = [];
		const sinks = {
			action: (a, v, cid) => calls.push({ a, v, cid }),
			shift: () => {
				throw new Error('unexpected shift');
			}
		};
		assert.equal(
			bindings.dispatchPractice(r, { pressed: true }, sinks),
			'action',
			`${id}: dispatch`
		);
		assert.equal(calls.length, 1, `${id}: one action call`);
		assert.deepEqual(calls[0].a, expected, `${id}: dispatched action`);
		assert.deepEqual(calls[0].v, expectedVelocity, `${id}: dispatched value`);
		assert.equal(calls[0].cid, id, `${id}: control id`);
	}
});

test('PAD FX and SAMPLER stay inert: their pad modes are unavailable (PRRT_kwDOSEvNd860tQ87)', () => {
	const padModeIds = ['d1-pad-fx', 'd2-pad-fx', 'd1-sampler', 'd2-sampler'];
	for (const id of padModeIds) {
		const r = byId(id);
		assert.equal(r.kind, 'inert', `${id} should stay inert`);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/, id);
	}
	for (const b of bindings.PRACTICE_WIRE_BINDINGS) {
		if (b.action.type === 'controller_pad_mode') {
			assert.equal(b.action.mode, 'hot_cue', 'supplement only hot_cue pad mode');
		}
	}
});

test('SHIFT layer does not pick up the supplement', () => {
	const beatSync = controlById('d1-beat-sync');
	const shifted = bindings.resolveControl(FLX4_MAP, beatSync, true);
	// Shifted wire is ch1 note 0x60: map hints only (BEAT SYNC + SHIFT), not the supplement row on note 88.
	assert.equal(shifted.kind, 'inert');
	assert.match(shifted.hint, /BEAT SYNC \+ SHIFT/);
	const otherMap = { ...FLX4_MAP, nameMatch: 'DDJ-FLX4-practice-negative' };
	const r = bindings.resolveControl(otherMap, beatSync, false);
	assert.equal(r.kind, 'inert');
});
