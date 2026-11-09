// requirement: SKIN-10486
// The CDJ-3000 practice panel joins to CDJ3000_MAP: one player, channel 1 wires.
//
//   if CDJ3000_MAP fails the core validator, or matches a CDJ-2000 or XDJ port then broken
//   if any panel control's wire identity is neither bound nor hinted by CDJ3000_MAP then broken
//   if PLAY/PAUSE does not fire deck_play_toggle deck 1 then broken
//   if HOT CUE C does not fire deck_hot_cue deck 1 slot C then broken
//   if 4 BEAT LOOP and 8 BEAT LOOP do not fire 4- and 8-beat loops then broken
//   if the TEMPO slider is not a 7-bit deck_pitch (no LSB pairing) then broken
//   if the jog is not a relative, 64-centred deck_jog then broken
//   if a hint collides with a bound source, or a source repeats within a list then broken
//   if the map claims LED rules the vendor list does not document then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/cdj-3000/panel.json');

let vite;
let bindings;
let webmidi;
let CDJ3000_MAP;
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
	({ CDJ3000_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/pioneer-cdj-3000.ts'));
});

after(async () => {
	await vite.close();
});

const byId = (id) => {
	const control = layout.controls.find((c) => c.id === id);
	assert.ok(control, `panel has ${id}`);
	return bindings.resolveControl(CDJ3000_MAP, control, false);
};

const key = (s) => `${s.ch}/${s.kind}/${s.id}`;

test('the map validates and matches only a CDJ-3000 port', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(CDJ3000_MAP);
	webmidi._resetMidiForTests();
	const re = new RegExp(CDJ3000_MAP.nameMatch, 'i');
	assert.ok(re.test('Pioneer DJ CDJ-3000'));
	assert.ok(!re.test('CDJ-2000NXS2'));
	assert.ok(!re.test('XDJ-RX3'));
});

test('every CDJ-3000 panel control resolves against CDJ3000_MAP', () => {
	const resolved = bindings.resolveLayout(CDJ3000_MAP, layout);
	assert.equal(resolved.length, 44);
	const bound = resolved.filter((r) => r.kind === 'bound').length;
	assert.equal(bound, 20, `bound ${bound}`);
	for (const c of layout.controls) assert.equal(c.shift_midi, null, `${c.id} has no shift row`);
});

test('wire identity picks the same action as the hardware', () => {
	assert.deepEqual(byId('play-pause').binding.action, { type: 'deck_play_toggle', deck: 1 });
	assert.deepEqual(byId('cue').binding.action, { type: 'deck_cue', deck: 1 });
	assert.deepEqual(byId('pad-3').binding.action, { type: 'deck_hot_cue', deck: 1, slot: 'C' });
	assert.deepEqual(byId('4-beat').binding.action, { type: 'deck_beat_loop', deck: 1, beats: 4 });
	assert.deepEqual(byId('8-beat').binding.action, { type: 'deck_beat_loop', deck: 1, beats: 8 });
	assert.deepEqual(byId('beat-sync').binding.action, { type: 'deck_sync_toggle', deck: 1 });
	assert.deepEqual(byId('key-sync').binding.action, { type: 'deck_key_sync_toggle', deck: 1 });
	assert.deepEqual(byId('in-cue').binding.action, { type: 'deck_manual_loop_cycle', deck: 1 });
	assert.deepEqual(byId('reloop-exit').binding.action, { type: 'deck_loop_exit', deck: 1 });
	const tempo = byId('tempo').binding;
	assert.deepEqual(tempo.action, { type: 'deck_pitch', deck: 1, lsbOffset: null });
	assert.deepEqual(tempo.source, { ch: 1, kind: 'cc', id: 29 });
	const jog = byId('jog').binding;
	assert.equal(jog.action.type, 'deck_jog');
	assert.equal(jog.relative, true);
	assert.equal(jog.relativeCentered, true);
	const browse = byId('browse').binding;
	assert.equal(browse.action.type, 'browse_encoder');
	assert.equal(browse.relative, true);
	assert.equal(browse.relativeCentered, undefined);
	assert.equal(byId('search-fwd').kind, 'inert');
	assert.match(bindings.controlTitle(byId('search-fwd')), /not implemented - see PARITY-TODO/);
	assert.match(byId('direction').hint, /DIRECTION REV/);
});

test('players 2 to 4 are the same wires on channels 2 to 4', () => {
	const play = CDJ3000_MAP.bindings.filter((b) => b.source.kind === 'note' && b.source.id === 0);
	assert.deepEqual(
		play.map((b) => [b.source.ch, b.action.deck]),
		[
			[1, 1],
			[2, 2],
			[3, 3],
			[4, 4]
		]
	);
	const load = CDJ3000_MAP.bindings.find((b) => b.source.ch === 2 && b.source.id === 17);
	assert.deepEqual(load.action, { type: 'browse_load', deck: 2 });
});

test('hints never collide with bound sources, and sources are unique within each list', () => {
	const bound = new Set(CDJ3000_MAP.bindings.map((b) => key(b.source)));
	assert.equal(bound.size, CDJ3000_MAP.bindings.length);
	const hints = CDJ3000_MAP.hints.map((h) => key(h.source));
	assert.equal(new Set(hints).size, hints.length);
	for (const h of hints) assert.ok(!bound.has(h), `hint ${h} is also bound`);
});

test('no LED rules: the vendor list documents MIDI-IN only', () => {
	assert.deepEqual(CDJ3000_MAP.leds, []);
});

test('a wire number the map does not know throws (negative control)', () => {
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(() => bindings.resolveControl(CDJ3000_MAP, bogus), /neither binds nor hints/);
});
