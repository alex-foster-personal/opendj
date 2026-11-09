// requirement: SKIN-10481
// The DDJ-1000 practice panel joins to DDJ1000_MAP, the map transcribed from the official
// DDJ-1000 List of MIDI message.
//
//   if any panel control's wire identity is neither bound nor hinted by DDJ1000_MAP then broken
//   if deck 1 PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if deck 2 pad 3 does not fire deck_hot_cue deck 2 slot C then broken
//   if channel 4 COLOR FX does not fire mixer_channel filter deck 4 then broken
//   if BEAT SYNC resolves to anything but deck_sync_toggle then broken
//   if LOOP IN or MASTER TEMPO resolve as bound while the map only hints them then broken
//   if a control with a wire number absent from the map resolves silently then broken
//   if TEMPO's value is not 14-bit (continuous14) then broken
//   if a documented shifted row does not resolve with shiftHeld then broken
//   if practiceMapFor does not return DDJ1000_MAP for the layout then broken
//   if the map lacks an LED rule for PLAY, CUE, loop, BEAT SYNC, CH CUE or the hot-cue pads then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/ddj-1000/panel.json');

let vite;
let bindings;
let devices;
let DDJ1000_MAP;
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
	({ DDJ1000_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-1000.ts'));
});

after(async () => {
	await vite.close();
});

const byId = (id, shiftHeld = false) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return bindings.resolveControl(DDJ1000_MAP, control, shiftHeld);
};

const sameSource = (a, b) => a.ch === b.ch && a.kind === b.kind && a.id === b.id;

test('every DDJ-1000 panel control resolves against DDJ1000_MAP', () => {
	const resolved = bindings.resolveLayout(DDJ1000_MAP, layout);
	assert.equal(resolved.length, 120);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	// Measured: per deck PLAY, CUE, JOG, TEMPO, BEAT SYNC, KEY SYNC, 4 BEAT LOOP/EXIT, SHIFT, HOT CUE
	// mode, BROWSE and 8 pads (17 x 2), per channel strip TRIM, HI, MID, LOW, COLOR FX, CUE and
	// fader (7 x 4), plus CROSSFADER, MASTER, MASTER CUE, HEADPHONES MIX and LEVEL (5) = 69.
	assert.equal(bound, 69, `bound ${bound}`);
});

test('wire identity picks the same action as the controller', () => {
	assert.deepEqual(byId('d1-play-pause').binding.action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(byId('d2-pad-3').binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'C' });
	assert.deepEqual(byId('ch4-color-fx').binding.action, {
		type: 'mixer_channel',
		deck: 4,
		target: 'filter'
	});
	assert.deepEqual(byId('d2-beat-sync').binding.action, { type: 'deck_sync_toggle', deck: 2 });
	assert.deepEqual(byId('d1-key-sync').binding.action, { type: 'deck_key_sync_toggle', deck: 1 });
	assert.deepEqual(byId('d1-4-beat-loop-exit').binding.action, {
		type: 'deck_beat_loop',
		deck: 1,
		beats: 4
	});
	assert.deepEqual(byId('d2-browse').binding.action, { type: 'browse_encoder' });
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.equal(byId('ch3-ch-cue').binding.action.type, 'channel_cue');
	assert.equal(byId('master-cue').binding.action.type, 'master_cue');
	assert.deepEqual(byId('d1-hot-cue').binding.action, {
		type: 'controller_pad_mode',
		deck: 1,
		mode: 'hot_cue'
	});
});

test('hinted-only controls are inert with the parity tooltip', () => {
	for (const id of [
		'd1-loop-in',
		'd1-master-tempo',
		'd2-deck-select',
		'd1-pad-fx1',
		'fx-on',
		'booth'
	]) {
		const r = byId(id);
		assert.equal(r.kind, 'inert', id);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/);
	}
	assert.match(byId('d1-loop-in').hint, /LOOP IN \(deck 1\)/);
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(DDJ1000_MAP, bogus), /neither binds nor hints/);
});

test('tempo sends a 14-bit value, knobs 7-bit', () => {
	assert.equal(
		bindings.inputValueFor(byId('d1-tempo').binding, { value01: 0.5 }).kind,
		'continuous14'
	);
	assert.equal(bindings.inputValueFor(byId('ch1-trim').binding, { value01: 1 }).raw, 127);
});

test('SHIFT layer: every documented shifted row resolves, bound or hinted', () => {
	const shifted = layout.controls.filter((c) => c.shift_midi !== null);
	assert.ok(shifted.length >= 80, `shifted rows ${shifted.length}`);
	for (const c of shifted) bindings.resolveControl(DDJ1000_MAP, c, true);
	// 4 BEAT LOOP/EXIT + SHIFT is the hardware-encoded loop exit row.
	assert.deepEqual(byId('d1-4-beat-loop-exit', true).binding.action, {
		type: 'deck_loop_exit',
		deck: 1
	});
	// PLAY + SHIFT is documented but unbound: inert on the shifted layer, bound on the base layer.
	assert.equal(byId('d1-play-pause', true).kind, 'inert');
	assert.equal(byId('d1-play-pause', false).kind, 'bound');
	// SHIFT itself has no shifted row.
	assert.equal(layout.controls.find((c) => c.id === 'd1-shift').shift_midi, null);
});

test('SHIFT updates modifier state and never reaches the action glue', () => {
	const calls = { action: [], shift: [] };
	const sinks = {
		action: (a) => calls.action.push(a.type),
		shift: (held) => calls.shift.push(held)
	};
	assert.equal(bindings.dispatchPractice(byId('d2-shift'), { pressed: true }, sinks), 'modifier');
	assert.equal(bindings.dispatchPractice(byId('d2-shift'), { pressed: false }, sinks), 'modifier');
	assert.deepEqual(calls.shift, [true, false]);
	assert.deepEqual(calls.action, []);
	assert.equal(bindings.dispatchPractice(byId('d2-cue'), { pressed: true }, sinks), 'action');
	assert.deepEqual(calls.action, ['deck_cue']);
});

test('practiceMapFor returns DDJ1000_MAP and the map has no binding/hint collisions', () => {
	assert.strictEqual(devices.practiceMapFor(layout), DDJ1000_MAP);
	for (const h of DDJ1000_MAP.hints) {
		assert.ok(
			!DDJ1000_MAP.bindings.some((b) => sameSource(b.source, h.source)),
			`hint ${h.label} collides with a binding`
		);
	}
	const keys = DDJ1000_MAP.bindings.map((b) => `${b.source.ch}/${b.source.kind}/${b.source.id}`);
	assert.equal(new Set(keys).size, keys.length, 'duplicate binding source');
});

test('LED rules cover the MIDI-OUT rows the panel draws', () => {
	const out = (ch, note) => DDJ1000_MAP.leds.find((r) => r.out.ch === ch && r.out.note === note);
	assert.equal(out(1, 0x0b).trigger.kind, 'deck_playing');
	assert.equal(out(2, 0x0c).trigger.kind, 'deck_loaded');
	assert.equal(out(1, 0x14).trigger.kind, 'loop_engaged');
	assert.equal(out(2, 0x58).trigger.kind, 'beat_sync_enabled');
	assert.equal(out(3, 0x54).trigger.kind, 'channel_cue_enabled');
	assert.deepEqual(out(1, 0x1b).trigger, { kind: 'pad_mode_selected', deck: 1, mode: 'hot_cue' });
	assert.deepEqual(out(10, 2).trigger, { kind: 'hot_cue_present', deck: 2, slot: 'C' });
	// Every LED rule lights a wire some panel control carries.
	const wires = new Set(layout.controls.map((c) => `${c.midi.ch}:${c.midi.id}`));
	for (const rule of DDJ1000_MAP.leds) {
		if (rule.out.ch <= 2 || rule.out.ch === 8 || rule.out.ch === 10) {
			assert.ok(
				wires.has(`${rule.out.ch}:${rule.out.note}`),
				`LED ${rule.out.ch}:${rule.out.note}`
			);
		}
	}
});
