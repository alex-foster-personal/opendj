// Hercules DJ Control AIR map acceptance at the production map/registry boundary.
//
//   if registerDeviceMap(HERCULES_DJ_CONTROL_AIR_MAP) throws then broken
//   if nameMatch matches AIR+ or AIR S then broken
//   if play/cue/sync/PFL/load notes drift from D.2 then broken
//   if EQ bands lack invert: true then broken
//   if jog uses relativeCentered then broken
//   if pitch LSB offset is not 36 or LSB CCs are bound then broken
//   if effect pads bind instead of hint then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi;
let air;
let registry;

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
	air = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-dj-control-air.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const MAP = () => air.HERCULES_DJ_CONTROL_AIR_MAP;

function binding(ch, kind, id) {
	return MAP().bindings.find(
		(candidate) =>
			candidate.source.ch === ch && candidate.source.kind === kind && candidate.source.id === id
	);
}

function hint(ch, kind, id) {
	return MAP().hints?.find(
		(candidate) =>
			candidate.source.ch === ch && candidate.source.kind === kind && candidate.source.id === id
	);
}

const DECK1 = {
	play: 0x12,
	cue: 0x11,
	sync: 0x13,
	listen: 0x14,
	load: 0x15,
	nudgeMinus: 0x0d,
	nudgePlus: 0x0e,
	loopPads: [0x09, 0x0a, 0x0b, 0x0c],
	jog: 0x30,
	jogScratch: 0x32,
	pitchMsb: 0x34,
	volume: 0x36,
	treble: 0x37,
	mid: 0x38,
	bass: 0x39
};

const DECK2 = {
	play: 0x28,
	cue: 0x27,
	sync: 0x29,
	listen: 0x2a,
	load: 0x2b,
	nudgeMinus: 0x23,
	nudgePlus: 0x24,
	loopPads: [0x1f, 0x20, 0x21, 0x22],
	jog: 0x31,
	jogScratch: 0x33,
	pitchMsb: 0x35,
	volume: 0x3b,
	treble: 0x3c,
	mid: 0x3d,
	bass: 0x3e
};

test('map registers and appears in DEVICE_MAP_REGISTRY', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(MAP());
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(MAP()));
	assert.equal(registry.DEVICE_MAP_REGISTRY.filter((m) => m === MAP()).length, 1);
	webmidi._resetMidiForTests();
});

test('nameMatch distinguishes AIR from AIR+ and AIR S', () => {
	const re = new RegExp(MAP().nameMatch, 'i');
	for (const ok of [
		'DJ Control AIR',
		'Hercules DJ Control AIR',
		'DJ Control AIR MIDI 1',
		'DJControl AIR'
	]) {
		assert.ok(re.test(ok), `expected match: ${ok}`);
	}
	for (const bad of ['DJ Control AIR+', 'DJControl AIR+', 'DJ Control AIR S', 'DJControl Air S']) {
		assert.equal(re.test(bad), false, `expected no match: ${bad}`);
	}

	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	assert.equal(webmidi.resolveMapForPort('Hercules DJ Control AIR')?.nameMatch, MAP().nameMatch);
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('transport and browse per deck', () => {
	for (const [deck, w] of [
		[1, DECK1],
		[2, DECK2]
	]) {
		assert.deepEqual(binding(1, 'note', w.play).action, { type: 'deck_play_toggle', deck });
		assert.deepEqual(binding(1, 'note', w.cue).action, { type: 'deck_cue', deck });
		assert.deepEqual(binding(1, 'note', w.sync).action, { type: 'deck_sync_toggle', deck });
		assert.deepEqual(binding(1, 'note', w.listen).action, { type: 'channel_cue', deck });
		assert.deepEqual(binding(1, 'note', w.load).action, { type: 'browse_load', deck });
	}
});

test('mixer faders, EQ and crossfader', () => {
	for (const [deck, w] of [
		[1, DECK1],
		[2, DECK2]
	]) {
		const fader = binding(1, 'cc', w.volume);
		assert.deepEqual(fader.action, { type: 'mixer_channel', deck, target: 'fader' });
		assert.equal(fader.invert, undefined);
		for (const [cc, band] of [
			[w.treble, 'high'],
			[w.mid, 'mid'],
			[w.bass, 'low']
		]) {
			const eq = binding(1, 'cc', cc);
			assert.deepEqual(eq.action, {
				type: 'mixer_channel',
				deck,
				target: 'eq',
				band
			});
			assert.equal(eq.invert, true);
		}
	}
	const xf = binding(1, 'cc', 0x3a);
	assert.deepEqual(xf.action, { type: 'mixer_global', target: 'crossfader' });
	assert.equal(xf.invert, undefined);
});

test('jog relative decode on both rotation CCs per deck', () => {
	for (const [deck, w] of [
		[1, DECK1],
		[2, DECK2]
	]) {
		for (const ccId of [w.jog, w.jogScratch]) {
			const b = binding(1, 'cc', ccId);
			assert.equal(b.relative, true);
			assert.notEqual(b.relativeCentered, true);
			assert.deepEqual(b.action, { type: 'deck_jog', deck });
		}
	}
});

test('pitch MSB with lsbOffset 36 and no LSB binding', () => {
	assert.equal(air.HERCULES_AIR_PITCH_LSB_OFFSET, 36);
	for (const [deck, msb] of [
		[1, 0x34],
		[2, 0x35]
	]) {
		assert.deepEqual(binding(1, 'cc', msb).action, {
			type: 'deck_pitch',
			deck,
			lsbOffset: 36
		});
	}
	assert.equal(binding(1, 'cc', 0x58), undefined);
	assert.equal(binding(1, 'cc', 0x59), undefined);
});

test('tempo nudge and loop pads', () => {
	for (const [deck, w] of [
		[1, DECK1],
		[2, DECK2]
	]) {
		assert.deepEqual(binding(1, 'note', w.nudgeMinus).action, {
			type: 'deck_tempo_nudge',
			deck,
			direction: -1
		});
		assert.deepEqual(binding(1, 'note', w.nudgePlus).action, {
			type: 'deck_tempo_nudge',
			deck,
			direction: 1
		});
		w.loopPads.forEach((id, i) => {
			assert.deepEqual(binding(1, 'note', id).action, {
				type: 'deck_beat_loop',
				deck,
				beats: air.HERCULES_AIR_LOOP_PAD_BEATS[i]
			});
		});
	}
	assert.deepEqual(air.HERCULES_AIR_LOOP_PAD_BEATS, [1, 2, 4, 8]);
});

test('effect pads and Effects mode stay hints only', () => {
	for (const id of [0x01, 0x02, 0x03, 0x04, 0x17, 0x18, 0x19, 0x1a]) {
		assert.equal(binding(1, 'note', id), undefined);
		assert.ok(hint(1, 'note', id), `expected hint for effect pad note ${id}`);
	}
	assert.equal(binding(1, 'note', 0x38), undefined);
	assert.ok(hint(1, 'note', 0x38), 'Effects mode should be a hint');
	for (const b of MAP().bindings) {
		assert.notEqual(b.action.type, 'controller_pad_mode');
		assert.notEqual(b.action.type, 'controller_pad');
		assert.notEqual(b.action.type, 'shift_modifier');
	}
});

test('hygiene: channel 1 only, no deck 3/4, unique sources, no leds', () => {
	const boundKeys = new Set();
	for (const b of MAP().bindings) {
		assert.equal(b.source.ch, 1);
		if (b.action.deck === 3 || b.action.deck === 4) {
			assert.fail('deck 3/4 binding on a 2-deck controller');
		}
		const key = `${b.source.ch}:${b.source.kind}:${b.source.id}`;
		assert.ok(!boundKeys.has(key), `duplicate binding ${key}`);
		boundKeys.add(key);
	}
	const hintKeys = new Set();
	for (const h of MAP().hints ?? []) {
		assert.equal(h.source.ch, 1);
		const key = `${h.source.ch}:${h.source.kind}:${h.source.id}`;
		assert.ok(!hintKeys.has(key), `duplicate hint ${key}`);
		assert.ok(!boundKeys.has(key), `hint collides with binding ${key}`);
		hintKeys.add(key);
	}
	assert.equal(MAP().leds, undefined);
});
