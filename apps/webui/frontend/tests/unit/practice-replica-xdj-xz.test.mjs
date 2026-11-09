// requirement: SKIN-XDJXZ
// The XDJ-XZ practice panel joins to XDJ_XZ_MAP, which extends XDJ_RX3_MAP with the XZ-only rows.
//
//   if XDJ_XZ_MAP fails the core validator, or matches an RX3 or AZ port then broken
//   if any panel control's wire identity is neither bound nor hinted by XDJ_XZ_MAP then broken
//   if deck 1 PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if deck 2 pad 3 does not fire deck_hot_cue deck 2 slot C then broken
//   if 4 BEAT does not fire a 4-beat loop, or its long-press row an 8-beat loop then broken
//   if a held SHIFT on a hot-cue pad resolves to anything but the inert shifted hint then broken
//   if ROTARY SELECTOR + SHIFT is not the same relative browse encoder then broken
//   if a hint collides with a bound source then broken
//   if CUE or 4 BEAT have no LED rule then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/xdj-xz/panel.json');

let vite;
let bindings;
let webmidi;
let XDJ_XZ_MAP;
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
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	({ XDJ_XZ_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/pioneer-xdj-xz.ts'));
});

after(async () => {
	await vite.close();
});

const byId = (id, shift = false) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return bindings.resolveControl(XDJ_XZ_MAP, control, shift);
};

const key = (s) => `${s.ch}/${s.kind}/${s.id}`;

test('the map validates and matches only an XZ port', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(XDJ_XZ_MAP);
	webmidi._resetMidiForTests();
	const re = new RegExp(XDJ_XZ_MAP.nameMatch, 'i');
	assert.ok(re.test('Pioneer DJ XDJ-XZ'));
	assert.ok(!re.test('XDJ-RX3'));
	assert.ok(!re.test('XDJ-AZ'));
});

test('every XDJ-XZ panel control resolves against XDJ_XZ_MAP', () => {
	const resolved = bindings.resolveLayout(XDJ_XZ_MAP, layout);
	assert.equal(resolved.length, 156);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	assert.ok(bound >= 70, `bound ${bound}`);
	for (const c of layout.controls) {
		if (c.shift_midi !== null) bindings.resolveControl(XDJ_XZ_MAP, c, true);
	}
});

test('wire identity picks the same action as the hardware', () => {
	assert.deepEqual(byId('d1-play-pause').binding.action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(byId('d2-pad-3').binding.action, { type: 'deck_hot_cue', deck: 2, slot: 'C' });
	assert.deepEqual(byId('d1-4-beat').binding.action, { type: 'deck_beat_loop', deck: 1, beats: 4 });
	assert.deepEqual(byId('ch4-ch-fader').binding.action, {
		type: 'mixer_channel',
		deck: 4,
		target: 'fader'
	});
	assert.equal(byId('ch2-ch-cue').binding.ccButton, true);
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.equal(byId('d1-jog').kind, 'bound');
	assert.equal(byId('fx-on-off').kind, 'inert');
	assert.match(bindings.controlTitle(byId('fx-on-off')), /not implemented - see PARITY-TODO/);
});

test('4 BEAT long press is the 8-beat row', () => {
	const long = XDJ_XZ_MAP.bindings.filter((b) => b.source.kind === 'note' && b.source.id === 68);
	assert.equal(long.length, 4);
	assert.deepEqual(long[1].action, { type: 'deck_beat_loop', deck: 2, beats: 8 });
});

test('SHIFT layer: pads go inert on their shifted row, the selector stays a browse encoder', () => {
	const pad = byId('d1-pad-1', true);
	assert.equal(pad.kind, 'inert');
	assert.deepEqual(pad.wire, { ch: 6, kind: 'note', id: 8 });
	assert.match(pad.hint, /PAD 1 \+ SHIFT/);
	const browse = byId('browse', true);
	assert.equal(browse.kind, 'bound');
	assert.equal(browse.binding.source.id, 96);
	assert.equal(browse.binding.action.type, 'browse_encoder');
	assert.equal(browse.binding.relative, true);
});

test('hints never collide with bound sources, and sources are unique within each list', () => {
	const bound = new Set(XDJ_XZ_MAP.bindings.map((b) => key(b.source)));
	assert.equal(bound.size, XDJ_XZ_MAP.bindings.length);
	const hints = XDJ_XZ_MAP.hints.map((h) => key(h.source));
	assert.equal(new Set(hints).size, hints.length);
	for (const h of hints) assert.ok(!bound.has(h), `hint ${h} is also bound`);
});

test('CUE and 4 BEAT have LED rules on their MIDI-OUT notes', () => {
	const leds = XDJ_XZ_MAP.leds;
	const cue = leds.find((r) => r.trigger.kind === 'deck_loaded' && r.trigger.deck === 2);
	assert.deepEqual(cue.out, { ch: 2, note: 1, velocityOn: 0x7f, velocityOff: 0x00 });
	const loop4 = leds.find((r) => r.trigger.kind === 'loop_beats_engaged' && r.trigger.deck === 1);
	assert.equal(loop4.trigger.beats, 4);
	assert.equal(loop4.out.note, 67);
	assert.ok(leds.some((r) => r.trigger.kind === 'hot_cue_present' && r.out.ch === 7));
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(XDJ_XZ_MAP, bogus), /neither binds nor hints/);
});
