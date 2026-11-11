// requirement: SKIN-XDJAZ
// The XDJ-AZ practice panel joins to XDJ_AZ_MAP, the map the real unit drives.
//
//   if any XDJ-AZ panel control's wire (base or SHIFT row) is neither bound nor hinted then broken
//   if left-deck PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if right-deck pad 7 does not fire deck_hot_cue deck 2 slot G on channel 11 then broken
//   if channel 4's COLOR knob does not fire mixer_channel filter deck 4 then broken
//   if the DECK SELECT box for deck 3 resolves as bound while the map only hints it then broken
//   if a shifted jog is not deck_jog scale 4 on its own deck then broken
//   if practiceMapFor cannot find XDJ_AZ_MAP then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/xdj-az/panel.json');

let vite;
let bindings;
let devices;
let XDJ_AZ_MAP;
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
	({ XDJ_AZ_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/pioneer-xdj.ts'));
});

after(async () => {
	await vite.close();
});

const byId = (id, shift = false) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return bindings.resolveControl(XDJ_AZ_MAP, control, shift);
};

test('the registry serves XDJ_AZ_MAP for the xdj-az layout', () => {
	assert.equal(devices.practiceMapFor(layout), XDJ_AZ_MAP);
});

test('every XDJ-AZ panel control resolves on both layers', () => {
	const resolved = bindings.resolveLayout(XDJ_AZ_MAP, layout);
	assert.equal(resolved.length, 172);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	assert.ok(bound >= 70, `bound ${bound}`);
	for (const control of layout.controls) {
		// A held SHIFT must never throw: the shifted row is a binding or a hint too.
		bindings.resolveControl(XDJ_AZ_MAP, control, true);
	}
});

test('wire identity picks the same action as the unit', () => {
	assert.deepEqual(byId('d1-play-pause').binding.action, { type: 'deck_play_toggle', deck: 1 });
	const pad7 = byId('d2-pad-7');
	assert.deepEqual(pad7.binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'G' });
	assert.deepEqual(pad7.wire, { ch: 11, kind: 'note', id: 38 });
	assert.deepEqual(byId('ch4-color').binding.action, {
		type: 'mixer_channel',
		deck: 4,
		target: 'filter'
	});
	assert.equal(byId('ch3-fader').binding.action.deck, 3);
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.equal(byId('d1-shift').binding.action.type, 'shift_modifier');
	assert.equal(byId('load-4').binding.action.deck, 4);
});

test('hinted-only controls are inert with the parity tooltip', () => {
	const select3 = byId('d1-deck-select-a');
	assert.equal(select3.kind, 'inert');
	assert.deepEqual(select3.wire, { ch: 3, kind: 'note', id: 21 });
	assert.match(bindings.controlTitle(select3), /not implemented - see PARITY-TODO/);
	assert.equal(byId('fx-on-off').kind, 'inert');
});

test('shifted jogs stay bound as deck_jog at scale 4', () => {
	for (const [id, deck] of [
		['d1-jog', 1],
		['d2-jog', 2]
	]) {
		const jog = byId(id, true);
		assert.equal(jog.kind, 'bound');
		assert.deepEqual(jog.binding.action, { type: 'deck_jog', deck, scale: 4 });
	}
});

const offsets = {
	hot_cue: [0, 1],
	beat_loop: [2, 3],
	pad_fx1_page1: [4, null],
	pad_fx1_page2: [5, null],
	pad_fx2_page1: [6, null],
	pad_fx2_page2: [7, null],
	slip_loop: [null, 8],
	beat_jump: [9, 10],
	gate_cue: [11, 12],
	beat_loop2: [13, 14],
	key_shift: [15, 16],
	beat_jump2: [17, 18]
};

test('all named layers resolve their exact wires, titles and existing actions', () => {
	for (const deck of [1, 2])
		for (let pad = 0; pad < 8; pad++) {
			const control = layout.controls.find((c) => c.id === `d${deck}-pad-${pad + 1}`);
			for (const [mode, rows] of Object.entries(offsets))
				for (const shift of [false, true]) {
					const r = bindings.resolveControl(
						XDJ_AZ_MAP,
						control,
						shift,
						{ [`d${deck}`]: mode },
						layout
					);
					const offset = rows[Number(shift)];
					assert.match(bindings.controlTitle(r), new RegExp(control.pad_layers[mode].label));
					if (offset === null) {
						assert.equal(r.kind, 'unwired');
						assert.equal(r.wire, null);
					} else {
						assert.deepEqual(r.wire, {
							ch: (pad < 4 ? 5 : 9) + deck,
							kind: 'note',
							id: 19 * (pad % 4) + offset
						});
						assert.match(bindings.controlTitle(r), new RegExp(`ch ${r.wire.ch} note ${r.wire.id}`));
						if (offset === 0 || offset === 2) {
							assert.equal(r.binding.action.type, offset === 0 ? 'deck_hot_cue' : 'deck_beat_loop');
						} else {
							assert.equal(r.kind, 'inert');
							assert.equal(
								bindings.dispatchPractice(
									r,
									{ pressed: true },
									{
										action() {
											assert.fail('unsupported action');
										}
									}
								),
								'inert'
							);
						}
					}
				}
		}
});

test('layer commands validate before mutation and agent query uses the real resolver', async () => {
	const layers = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-pad-layers.ts');
	let selection = layers.initialPadLayers(layout);
	selection = layers.selectPadLayer(layout, selection, {
		type: 'select',
		group: 'd1',
		mode: 'beat_jump'
	});
	assert.deepEqual(selection, { d1: 'beat_jump', d2: 'hot_cue' });
	for (const cmd of [
		null,
		{},
		{ type: 'select', group: 'absent', mode: 'hot_cue' },
		{ type: 'select', group: 'd1', mode: 'oops' }
	]) {
		assert.throws(() => layers.selectPadLayer(layout, selection, cmd));
	}
	const q = layers.queryPadLayers(layout, XDJ_AZ_MAP, selection, true);
	assert.equal(q.device, 'xdj-az');
	assert.equal(q.shift, true);
	assert.deepEqual(q.pads.find((p) => p.id === 'd1-pad-1').wire, { ch: 6, kind: 'note', id: 10 });
	assert.deepEqual(JSON.parse(JSON.stringify(q)), q);
	const broken = structuredClone(layout);
	broken.pad_layer_groups.d1.default_mode = 'missing';
	assert.throws(() => layers.initialPadLayers(broken));
	broken.pad_layer_groups.d1.default_mode = 'hot_cue';
	broken.controls.find((c) => c.id === 'd1-pad-1').pad_layers.beat_jump.midi.id = 127;
	assert.throws(
		() => bindings.resolveLayout(XDJ_AZ_MAP, broken, false, selection),
		/neither binds nor hints/
	);
	const incomplete = structuredClone(layout);
	delete incomplete.controls.find((c) => c.id === 'd1-pad-1').pad_layers.hot_cue.shift_midi;
	assert.throws(() => layers.initialPadLayers(incomplete));
});

test('IPC validates input, serializes state and cleans up only its own installation', async () => {
	const layers = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-pad-layers.ts');
	const ipcModule = await vite.ssrLoadModule(
		'/src/lib/components/rb/practice/practice-pad-layers-ipc.ts'
	);
	let selection = layers.initialPadLayers(layout);
	const query = () => layers.queryPadLayers(layout, XDJ_AZ_MAP, selection, false);
	const ipc = ipcModule.createPracticeLayersIpc({
		query,
		dispatch(cmd) {
			selection = layers.selectPadLayer(layout, selection, cmd);
			return query();
		}
	});
	assert.throws(() => ipc.dispatch({ type: 'select', group: 1, mode: 'beat_jump' }));
	assert.equal(
		ipc.dispatch({ type: 'select', group: 'd2', mode: 'beat_jump' }).groups.d2.selected_mode,
		'beat_jump'
	);
	globalThis.window = {};
	try {
		const cleanup = ipcModule.installPracticeLayersIpc(ipc);
		assert.equal(window.musicDjToolsPracticeLayers, ipc);
		assert.throws(() => ipcModule.installPracticeLayersIpc(ipc), /already installed/);
		cleanup();
		assert.equal(window.musicDjToolsPracticeLayers, undefined);
	} finally {
		delete globalThis.window;
	}
});

test('held pads release the original action exactly once across mode and SHIFT changes', () => {
	const control = layout.controls.find((c) => c.id === 'd1-pad-1');
	const calls = [];
	const holds = bindings.createPracticeHolds({
		action: (action, value) => calls.push({ action, value })
	});
	const hotCue = bindings.resolveControl(XDJ_AZ_MAP, control, false, { d1: 'hot_cue' }, layout);
	holds.press(hotCue);
	const unavailable = bindings.resolveControl(
		XDJ_AZ_MAP,
		control,
		true,
		{ d1: 'pad_fx1_page1' },
		layout
	);
	assert.equal(unavailable.kind, 'unwired');
	holds.press(unavailable); // repeated pointerdown cannot replace a held snapshot
	holds.release(control.id); // pointerup
	holds.release(control.id); // lost capture
	holds.releaseAll(); // unmount
	assert.equal(calls.length, 2);
	assert.deepEqual(
		calls.map((c) => c.action),
		[hotCue.binding.action, hotCue.binding.action]
	);
	assert.deepEqual(
		calls.map((c) => c.value.pressed),
		[true, false]
	);
	holds.press(hotCue);
	holds.releaseAll(); // cancellation/unmount without pointerup
	holds.release(control.id);
	assert.deepEqual(
		calls.map((c) => c.value.pressed),
		[true, false, true, false]
	);
});

test('direct selected resolution requires group metadata instead of silently using HOT CUE', () => {
	const pad = layout.controls.find((c) => c.id === 'd1-pad-1');
	assert.throws(
		() => bindings.resolveControl(XDJ_AZ_MAP, pad, false, { d1: 'beat_jump' }),
		/requires its panel layout/
	);
	assert.throws(
		() => bindings.resolveControl(XDJ_AZ_MAP, pad, false, { d1: 'missing' }, layout),
		/unknown pad layer/
	);
});
