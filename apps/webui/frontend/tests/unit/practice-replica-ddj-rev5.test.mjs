// requirement: SKIN-10439
// The DDJ-REV5 practice panel joins to REV5_MAP, the map the real controller drives.
//
//   if registerDeviceMap(REV5_MAP) throws then broken
//   if nameMatch matches DDJ-REV7 or DDJ-REV1 then broken
//   if any panel control's base or shifted wire is neither bound nor hinted by REV5_MAP then broken
//   if deck 1 PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if deck 2 pad 3 does not fire deck_hot_cue deck 2 slot C then broken
//   if FILTER on channel 2 does not fire mixer_channel filter deck 2 on channel 7 then broken
//   if SHIFT is not the channel-7 shift_modifier then broken
//   if the jog is not a relative centred deck_jog then broken
//   if a hint collides with a bound source then broken
//   if an LED rule points at a wire no panel control carries then broken
//   if practiceMapFor does not find REV5_MAP for the ddj-rev5 layout then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/ddj-rev5/panel.json');

let vite;
let bindings;
let devices;
let webmidi;
let REV5_MAP;
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
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	({ REV5_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-rev5.ts'));
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;
const byId = (id, shift = false) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return bindings.resolveControl(REV5_MAP, control, shift);
};

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(REV5_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers DDJ-REV5 port names only', () => {
	const re = new RegExp(REV5_MAP.nameMatch, 'i');
	assert.ok(re.test('DDJ-REV5'));
	assert.ok(re.test('Pioneer DJ DDJ-REV5 MIDI 1'));
	assert.ok(!re.test('DDJ-REV7'));
	assert.ok(!re.test('DDJ-REV1'));
	assert.ok(!re.test('DDJ-FLX4'));
});

test('every REV5 panel control resolves on both layers, most of them bound', () => {
	const resolved = bindings.resolveLayout(REV5_MAP, layout);
	assert.equal(resolved.length, 112);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	assert.ok(bound >= 55, `bound ${bound}`);
	for (const control of layout.controls) {
		if (control.shift_midi !== null) bindings.resolveControl(REV5_MAP, control, true);
	}
});

test('wire identity picks the same action as the controller', () => {
	assert.deepEqual(byId('d1-play-pause').binding.action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(byId('d2-pad-3').binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'C' });
	const filter = byId('ch2-filter');
	assert.deepEqual(filter.binding.action, { type: 'mixer_channel', deck: 2, target: 'filter' });
	assert.deepEqual(filter.wire, { ch: 7, kind: 'cc', id: 0x18 });
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.deepEqual(byId('d2-sync').binding.action, { type: 'deck_sync_toggle', deck: 2 });
	assert.deepEqual(byId('d1-loop-half').binding.action, {
		type: 'deck_loop_scale',
		deck: 1,
		factor: 0.5
	});
	assert.deepEqual(byId('d1-key-minus', true).binding.action, {
		type: 'deck_key_sync_toggle',
		deck: 1
	});
});

test('SHIFT is the one channel-7 modifier and never reaches the action glue', () => {
	const shift = byId('shift');
	assert.deepEqual(shift.wire, { ch: 7, kind: 'note', id: 0x3f });
	const calls = [];
	const sinks = { action: (a) => calls.push(a.type), shift: (held) => calls.push(`shift:${held}`) };
	assert.equal(bindings.dispatchPractice(shift, { pressed: true }, sinks), 'modifier');
	assert.deepEqual(calls, ['shift:true']);
	assert.equal(REV5_MAP.bindings.filter((b) => b.action.type === 'shift_modifier').length, 1);
});

test('jog is a relative centred deck_jog; TEMPO is 14-bit', () => {
	const jog = byId('d1-jog');
	assert.equal(jog.kind, 'bound');
	assert.equal(jog.binding.action.type, 'deck_jog');
	assert.equal(jog.binding.relativeCentered, true);
	assert.equal(
		bindings.inputValueFor(byId('d1-tempo').binding, { value01: 0.5 }).kind,
		'continuous14'
	);
});

test('hinted-only controls are inert with the parity tooltip', () => {
	const slip = byId('d1-slip');
	assert.equal(slip.kind, 'inert');
	assert.match(bindings.controlTitle(slip), /not implemented - see PARITY-TODO/);
	assert.equal(byId('fx-lever-1').kind, 'inert');
});

test('two-channel controller: nothing binds deck 3 or deck 4', () => {
	assert.deepEqual(
		REV5_MAP.bindings.filter((b) => 'deck' in b.action && b.action.deck > 2),
		[]
	);
});

test('hints never collide with bound sources, and sources are unique', () => {
	const bound = new Set(REV5_MAP.bindings.map((b) => key(b.source)));
	assert.equal(bound.size, REV5_MAP.bindings.length);
	const hinted = REV5_MAP.hints.map((h) => key(h.source));
	assert.equal(new Set(hinted).size, hinted.length);
	for (const h of hinted) assert.ok(!bound.has(h), `hint ${h} is also bound`);
});

test('every LED rule lights a wire some panel control carries', () => {
	const wires = new Set(layout.controls.map((c) => `${c.midi.ch}:${c.midi.id}`));
	for (const rule of REV5_MAP.leds) {
		assert.ok(wires.has(`${rule.out.ch}:${rule.out.note}`), `LED ${rule.out.ch}:${rule.out.note}`);
	}
	const play = REV5_MAP.leds.find((l) => l.trigger.kind === 'deck_playing' && l.trigger.deck === 1);
	assert.deepEqual(play.out, { ch: 1, note: 0x0b, velocityOn: 0x7f, velocityOff: 0x00 });
});

test('practice-devices registry serves REV5_MAP for the ddj-rev5 layout', () => {
	assert.equal(devices.practiceMapFor(layout), REV5_MAP);
});
