// requirement: SKIN-10483
// Wire numbers are read off the official OMNIS-DUO MIDI Message List (E1); the practice
// panel joins to OMNIS_DUO_MAP, the map a real OMNIS-DUO would drive.
//
//   if OMNIS_DUO_MAP fails the core validator or is missing from the registry then broken
//   if play/cue/tempo/fader/PFL/pad wires drift from the list then broken
//   if CH CUE stops decoding as a CC button, or the jog as centred relative then broken
//   if any panel control's wire identity is neither bound nor hinted by OMNIS_DUO_MAP then broken
//   if the bound count is not exactly 45 of 66 then broken
//   if deck 2 pad 3 does not fire deck_hot_cue deck 2 slot C then broken
//   if JOG, BEAT JUMP, Beat FX, MIC, BOOTH or the Bluetooth section resolve as bound then broken
//   if SHIFT + BEAT < does not resolve the shifted hint wire then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/omnis-duo/panel.json');
const layout = JSON.parse(readFileSync(PANEL, 'utf8'));

/** Controls the map only hints; one reason each (no MidiAction exists, or the replica cannot drive it). */
const SETTLED_INERT = {
	'd1-beat-jump-rev': 'The MidiAction union has no beat jump action.',
	'd1-beat-jump-fwd': 'The MidiAction union has no beat jump action.',
	'd2-beat-jump-rev': 'The MidiAction union has no beat jump action.',
	'd2-beat-jump-fwd': 'The MidiAction union has no beat jump action.',
	'd1-beat-loop':
		'The turn picks a loop length; deck_loop_scale carries a fixed factor, so a delta cannot drive it.',
	'd2-beat-loop':
		'The turn picks a loop length; deck_loop_scale carries a fixed factor, so a delta cannot drive it.',
	'd1-jog':
		'The panel jog carries the touch wire; rotation (deck_jog) needs relative motion the replica jog does not send.',
	'd2-jog':
		'The panel jog carries the touch wire; rotation (deck_jog) needs relative motion the replica jog does not send.',
	booth: 'The app has no booth output.',
	'mic1-level': 'The app has no microphone channel or mic MIDI action.',
	'mic2-level': 'The app has no microphone channel or mic MIDI action.',
	'mic-on': 'The app has no microphone channel or mic MIDI action.',
	'beat-prev': 'Beat FX are outside the MIDI contract; no fx_* action exists.',
	'beat-next': 'Beat FX are outside the MIDI contract; no fx_* action exists.',
	'level-depth': 'Beat FX are outside the MIDI contract; no fx_* action exists.',
	'fx-on': 'Beat FX are outside the MIDI contract; no fx_* action exists.',
	'bt-pairing': 'The Bluetooth source is hardware audio; the app has no action for it.',
	'bt-phones-cue': 'The Bluetooth source is hardware audio; the app has no action for it.',
	'bt-rev': 'The Bluetooth source is hardware audio; the app has no action for it.',
	'bt-play-pause': 'The Bluetooth source is hardware audio; the app has no action for it.',
	'bt-fwd': 'The Bluetooth source is hardware audio; the app has no action for it.'
};

let vite;
let webmidi;
let maps;
let registry;
let bindings;
let devices;

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
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	maps = await vite.ssrLoadModule('/src/lib/rb/midi/maps/alphatheta-omnis-duo.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
	bindings = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-bindings.ts');
	devices = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-devices.ts');
});

after(async () => {
	await vite.close();
});

const one = (pred) => {
	const hits = maps.OMNIS_DUO_MAP.bindings.filter(pred);
	assert.equal(hits.length, 1, `expected 1 binding, got ${hits.length}`);
	return hits[0];
};

const controlById = (id) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return control;
};

const byId = (id, shift = false) =>
	bindings.resolveControl(maps.OMNIS_DUO_MAP, controlById(id), shift);

test('the map registers, matches its port name and sits in both registries', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(maps.OMNIS_DUO_MAP);
	webmidi._resetMidiForTests();
	assert.ok(new RegExp(maps.OMNIS_DUO_MAP.nameMatch, 'i').test('OMNIS-DUO'));
	assert.ok(!new RegExp(maps.OMNIS_DUO_MAP.nameMatch, 'i').test('XDJ-AZ'));
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(maps.OMNIS_DUO_MAP));
	assert.equal(devices.PRACTICE_DEVICE_MAPS.OMNIS_DUO_MAP, maps.OMNIS_DUO_MAP);
	assert.equal(devices.practiceMapFor(layout), maps.OMNIS_DUO_MAP);
});

test('deck, mixer and pad wires match the list', () => {
	for (const deck of [1, 2]) {
		const play = one((b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck);
		assert.deepEqual(play.source, { ch: deck, kind: 'note', id: 0 });
		const cue = one((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.deepEqual(cue.source, { ch: deck, kind: 'note', id: 1 });
		const tempo = one((b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
		assert.deepEqual(tempo.source, { ch: deck, kind: 'cc', id: 0 });
		assert.equal(tempo.action.lsbOffset, 32);
		const load = one((b) => b.action.type === 'browse_load' && b.action.deck === deck);
		assert.deepEqual(load.source, { ch: deck, kind: 'note', id: 81 });
		const jogTop = one(
			(b) => b.action.type === 'deck_jog' && b.action.deck === deck && b.source.id === 34
		);
		assert.equal(jogTop.relativeCentered, true);
		const padH = one(
			(b) => b.action.type === 'deck_hot_cue' && b.action.deck === deck && b.action.slot === 'H'
		);
		assert.deepEqual(padH.source, { ch: 5 + deck, kind: 'note', id: 7 });
	}
	const fader2 = one((b) => b.action.target === 'fader' && b.action.deck === 2);
	assert.deepEqual(fader2.source, { ch: 5, kind: 'cc', id: 18 });
	const low1 = one((b) => b.action.band === 'low' && b.action.deck === 1);
	assert.deepEqual(low1.source, { ch: 5, kind: 'cc', id: 4 });
	const pfl = one((b) => b.action.type === 'channel_cue' && b.action.deck === 2);
	assert.deepEqual(pfl.source, { ch: 5, kind: 'cc', id: 71 });
	assert.equal(pfl.ccButton, true);
	const xf = one((b) => b.action.target === 'crossfader');
	assert.deepEqual(xf.source, { ch: 5, kind: 'cc', id: 11 });
	const browse = maps.OMNIS_DUO_MAP.bindings.filter((b) => b.action.type === 'browse_encoder');
	assert.deepEqual(
		browse.map((b) => b.source.id).sort((a, b) => a - b),
		[79, 96]
	);
	assert.ok(browse.every((b) => b.relative === true && b.relativeCentered !== true));
	assert.deepEqual(
		maps.OMNIS_DUO_MAP.meters.map((m) => m.out),
		[
			{ ch: 1, cc: 2, maxValue: 127 },
			{ ch: 2, cc: 2, maxValue: 127 }
		]
	);
});

test('every panel control resolves against OMNIS_DUO_MAP; 45 bound, the rest settled inert', () => {
	const resolved = bindings.resolveLayout(maps.OMNIS_DUO_MAP, layout);
	assert.equal(resolved.length, 66);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	assert.equal(bound, 45, `bound ${bound}`);
	const inertIds = resolved
		.filter((r) => r.kind === 'inert')
		.map((r) => r.control.id)
		.sort();
	assert.deepEqual(inertIds, Object.keys(SETTLED_INERT).sort());
	for (const id of inertIds) {
		assert.ok(SETTLED_INERT[id].length > 0, id);
		assert.match(bindings.controlTitle(byId(id)), /not implemented - see PARITY-TODO/, id);
	}
	// Every shifted row the panel carries is a binding or a hint too.
	for (const c of layout.controls) {
		if (c.shift_midi !== null) bindings.resolveControl(maps.OMNIS_DUO_MAP, c, true);
	}
});

test('wire identity picks the same action as the controller', () => {
	assert.deepEqual(byId('d1-play-pause').binding.action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(byId('d2-pad-3').binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'C' });
	assert.deepEqual(byId('ch2-color').binding.action, {
		type: 'mixer_channel',
		deck: 2,
		target: 'filter'
	});
	assert.deepEqual(byId('ch1-ch-cue').binding.action, { type: 'channel_cue', deck: 1 });
	assert.deepEqual(byId('load-2').binding.action, { type: 'browse_load', deck: 2 });
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.equal(
		bindings.inputValueFor(byId('d1-tempo').binding, { value01: 0.5 }).kind,
		'continuous14'
	);
	assert.match(bindings.controlTitle(byId('d1-cue')), /Fires deck_cue \(deck 1\)/);
});

test('SHIFT layer: shifted rows resolve like the hardware', () => {
	// BROWSE + SHIFT is the same relative encoder on CC 96.
	assert.equal(byId('browse', true).binding.source.id, 96);
	// Pad + SHIFT (hot cue delete) is a hint on notes 8-15 of the pad channel.
	const padShift = byId('d1-pad-1', true);
	assert.equal(padShift.kind, 'inert');
	assert.deepEqual(padShift.wire, { ch: 6, kind: 'note', id: 8 });
	// BEAT < + SHIFT is the hinted CC 120 row.
	const beatShift = byId('beat-prev', true);
	assert.equal(beatShift.kind, 'inert');
	assert.match(beatShift.hint, /beat left \+ SHIFT/);
	// PLAY has no documented shifted row, so SHIFT leaves it alone.
	assert.equal(controlById('d1-play-pause').shift_midi, null);
	assert.deepEqual(byId('d1-play-pause', true).binding.action, {
		type: 'deck_play_toggle',
		deck: 1
	});
	// Replica SHIFT is the modifier, never the action glue.
	const calls = { action: [], shift: [] };
	const sinks = {
		action: (a) => calls.action.push(a.type),
		shift: (held) => calls.shift.push(held)
	};
	assert.equal(bindings.dispatchPractice(byId('d2-shift'), { pressed: true }, sinks), 'modifier');
	assert.equal(bindings.dispatchPractice(byId('d1-jog'), { pressed: true }, sinks), 'inert');
	assert.deepEqual(calls.shift, [true]);
	assert.deepEqual(calls.action, []);
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(
		() => bindings.resolveControl(maps.OMNIS_DUO_MAP, bogus),
		/neither binds nor hints/
	);
});
