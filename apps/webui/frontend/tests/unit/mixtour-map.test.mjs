// Unit tests for the Reloop Mixtour P0 device map
// (src/lib/rb/midi/maps/reloop-mixtour.ts). Same vite+svelte bootstrap as
// midi-core.test.mjs (rune modules in the import graph need the plugin).
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(RELOOP_MIXTOUR_MAP) throws then broken (dupes/invalid)
//   if nameMatch accepts 'Mixtour Pro' then the unsafe legacy collision is back
//   if play deck1 isn't note ch1 0x0C ([S4]) then broken
//   if EQ CCs aren't 0x01=high 0x02=mid 0x03=low ([S4]) then broken
//   if pitch isn't pitchbend with lsbOffset null then broken
//   if browse encoder bindings aren't relative then broken
//   if hot-cue LEDs aren't 8 binary 0x7F/0x00 rules on 0x0D..0x10 then broken
//   if VU steps drift from the [S4] velocity table then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi;
let mapModule;

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/reloop-mixtour.ts');
});

after(async () => {
	await vite.close();
});

function findBindings(pred) {
	return mapModule.RELOOP_MIXTOUR_MAP.bindings.filter(pred);
}

// -------------------------------------------------------------- validation

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	// registerDeviceMap runs _buildIndex: throws on duplicate (shift,ch,kind,id)
	// keys, invalid nameMatch regex, or malformed 14-bit pitch bindings.
	webmidi.registerDeviceMap(mapModule.RELOOP_MIXTOUR_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers classic Mixtour and explicitly excludes Mixtour Pro', () => {
	const re = new RegExp(mapModule.RELOOP_MIXTOUR_MAP.nameMatch, 'i');
	assert.ok(re.test('Reloop Mixtour'));
	assert.ok(!re.test('Reloop Mixtour Pro'));
	assert.ok(!re.test('Mixtour Pro'));
	assert.ok(re.test('MIXTOUR MIDI 1'));
	assert.ok(!re.test('DDJ-FLX10'));
});

// ------------------------------------------------------- cited spot checks

test('transport: play/cue per deck on notes 0x0C/0x0B ch1/ch2 ([S4])', () => {
	for (const [ch, deck] of [
		[1, 1],
		[2, 2]
	]) {
		const play = findBindings(
			(b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck
		);
		assert.equal(play.length, 1);
		assert.deepEqual(play[0].source, { ch, kind: 'note', id: 0x0c });
		const cue = findBindings((b) => b.action.type === 'deck_cue' && b.action.deck === deck);
		assert.equal(cue.length, 1);
		assert.deepEqual(cue[0].source, { ch, kind: 'note', id: 0x0b });
	}
});

test('PFL per deck on note 0x03 ch1/ch2 ([S4])', () => {
	for (const [ch, deck] of [
		[1, 1],
		[2, 2]
	]) {
		const pfl = findBindings((b) => b.action.type === 'channel_cue' && b.action.deck === deck);
		assert.equal(pfl.length, 1);
		assert.deepEqual(pfl[0].source, { ch, kind: 'note', id: 0x03 });
	}
});

test('hot cues: 4 per deck, notes 0x0D..0x10, slots A..D ([S4])', () => {
	for (const [ch, deck] of [
		[1, 1],
		[2, 2]
	]) {
		const cues = findBindings((b) => b.action.type === 'deck_hot_cue' && b.action.deck === deck);
		assert.equal(cues.length, 4);
		const bySlot = Object.fromEntries(cues.map((b) => [b.action.slot, b.source]));
		assert.deepEqual(bySlot.A, { ch, kind: 'note', id: 0x0d });
		assert.deepEqual(bySlot.B, { ch, kind: 'note', id: 0x0e });
		assert.deepEqual(bySlot.C, { ch, kind: 'note', id: 0x0f });
		assert.deepEqual(bySlot.D, { ch, kind: 'note', id: 0x10 });
	}
});

test('mixer: trim CC 0x00, EQ 0x01=high 0x02=mid 0x03=low, fader 0x05 ([S4])', () => {
	for (const [ch, deck] of [
		[1, 1],
		[2, 2]
	]) {
		const strip = findBindings((b) => b.action.type === 'mixer_channel' && b.action.deck === deck);
		const byId = Object.fromEntries(strip.map((b) => [b.source.id, b.action]));
		assert.equal(byId[0x00].target, 'trim');
		assert.equal(byId[0x01].target, 'eq');
		assert.equal(byId[0x01].band, 'high');
		assert.equal(byId[0x02].band, 'mid');
		assert.equal(byId[0x03].band, 'low');
		assert.equal(byId[0x05].target, 'fader');
		for (const b of strip) assert.equal(b.source.ch, ch);
	}
});

test('globals: crossfader on CC 0x08 AND 0x12, master on 0x0F, all ch1 ([S4])', () => {
	const xf = findBindings(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader'
	);
	assert.deepEqual(
		xf.map((b) => b.source.id).sort((a, z) => a - z),
		[0x08, 0x12]
	);
	const master = findBindings(
		(b) => b.action.type === 'mixer_global' && b.action.target === 'master'
	);
	assert.equal(master.length, 1);
	assert.deepEqual(master[0].source, { ch: 1, kind: 'cc', id: 0x0f });
});

test('pitch: pitchbend source per deck, lsbOffset null (single 14-bit msg)', () => {
	const pitch = findBindings((b) => b.action.type === 'deck_pitch');
	assert.equal(pitch.length, 2);
	for (const b of pitch) {
		assert.equal(b.source.kind, 'pitchbend');
		assert.equal(b.action.lsbOffset, null);
	}
});

test('browse: encoder CCs 0x07/0x13 relative, load buttons both layers ([S4])', () => {
	const enc = findBindings((b) => b.action.type === 'browse_encoder');
	assert.deepEqual(
		enc.map((b) => b.source.id).sort((a, z) => a - z),
		[0x07, 0x13]
	);
	for (const b of enc) assert.equal(b.relative, true);
	const loads = findBindings((b) => b.action.type === 'browse_load');
	// note 0x02 on ch1/ch2 + alternate layer notes 0x13/0x4F on ch1
	assert.equal(loads.length, 4);
	assert.equal(loads.filter((b) => b.action.deck === 1).length, 2);
	assert.equal(loads.filter((b) => b.action.deck === 2).length, 2);
});

test('loops: beat-loop notes 0x11/0x4D ch1 + reloop-exit 0x09 per deck ([S4])', () => {
	const beatLoops = findBindings((b) => b.action.type === 'deck_beat_loop');
	assert.equal(beatLoops.length, 2);
	const byDeck = Object.fromEntries(beatLoops.map((b) => [b.action.deck, b]));
	assert.deepEqual(byDeck[1].source, { ch: 1, kind: 'note', id: 0x11 });
	assert.deepEqual(byDeck[2].source, { ch: 1, kind: 'note', id: 0x4d });
	for (const b of beatLoops) assert.equal(b.action.beats, 4);
	const exits = findBindings((b) => b.action.type === 'deck_loop_exit');
	assert.equal(exits.length, 2);
});

// ------------------------------------------------------------ led feedback

test('hot-cue LEDs: 8 binary rules mirroring pad notes, 0x7F/0x00 ([S4])', () => {
	const leds = mapModule.RELOOP_MIXTOUR_MAP.leds;
	assert.equal(leds.length, 8);
	for (const rule of leds) {
		assert.equal(rule.trigger.kind, 'hot_cue_present');
		assert.ok(rule.out.note >= 0x0d && rule.out.note <= 0x10);
		assert.equal(rule.out.velocityOn, 0x7f);
		assert.equal(rule.out.velocityOff, 0x00);
		assert.equal(rule.out.ch, rule.trigger.deck);
	}
});

// ---------------------------------------------------------------- vu data

test('VU outputs and velocity steps match the [S4] table verbatim', () => {
	assert.equal(mapModule.MIXTOUR_VU_OUTPUTS.length, 4);
	const byTarget = Object.fromEntries(mapModule.MIXTOUR_VU_OUTPUTS.map((o) => [o.target, o]));
	assert.deepEqual(byTarget.master_left, { target: 'master_left', ch: 1, note: 0x12 });
	assert.deepEqual(byTarget.master_right, { target: 'master_right', ch: 2, note: 0x12 });
	assert.deepEqual(byTarget.deck1, { target: 'deck1', ch: 1, note: 0x11 });
	assert.deepEqual(byTarget.deck2, { target: 'deck2', ch: 2, note: 0x11 });
	assert.deepEqual(
		mapModule.MIXTOUR_VU_VELOCITY_STEPS.map((s) => [s.min01, s.velocity]),
		[
			[0.98, 0x7f],
			[0.85, 0x67],
			[0.65, 0x4d],
			[0.4, 0x33],
			[0.01, 0x19],
			[0.0, 0x00]
		]
	);
});
