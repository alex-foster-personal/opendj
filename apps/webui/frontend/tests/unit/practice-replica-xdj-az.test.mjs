// requirement: SKIN-XDJAZ
// The XDJ-AZ practice panel joins to XDJ_AZ_MAP, the map the real unit drives.
//
//   if any XDJ-AZ panel control's wire (base or SHIFT row) is neither bound nor hinted then broken
//   if left-deck PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if right-deck pad 7 does not fire deck_hot_cue deck 2 slot G on channel 11 then broken
//   if channel 4's COLOR knob does not fire mixer_channel filter deck 4 then broken
//   if the DECK SELECT box for deck 3 resolves as bound while the map only hints it then broken
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
	assert.equal(byId('d2-jog', true).kind, 'inert');
});
