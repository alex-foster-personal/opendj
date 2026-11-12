// requirement: MIDI-11961
// Unit tests for the Hercules DJControl Inpulse 200 device map
// (src/lib/rb/midi/maps/hercules-djcontrol-inpulse-200.ts).
//
// Regression lines:
//   if registerDeviceMap(HERCULES_INPULSE_200_MAP) throws then broken
//   if nameMatch claims an Inpulse 200 MK2/MK3, 300, 500 or 2000 then broken
//   if any bound or hinted (ch, kind, id) is absent from the [CHART] allow-list then broken
//   if play, cue, sync, faders, crossfader or jogs are unbound then broken
//   if FILTER CC 01 is not the colour FX filter on both decks then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi;
let mod;
let map;

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	mod = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-djcontrol-inpulse-200.ts');
	map = mod.HERCULES_INPULSE_200_MAP;
});

after(async () => {
	await vite.close();
});

// ------------------------------------------------- [CHART] allow-list

const range = (lo, hi) => Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

/** Every INPUT row of the Hercules V1.0 chart, as (ch, kind, ids). */
const CHART_INPUT = new Set(
	[
		[1, 'note', [0x00, 0x01, 0x02, 0x03]],
		[1, 'cc', [0x00, 0x01, 0x02, 0x03, 0x04]],
		[2, 'note', range(0x03, 0x12)],
		[3, 'note', range(0x03, 0x12)],
		[2, 'cc', [0x00, 0x01, 0x02, 0x04, 0x05, 0x08, 0x09, 0x0a]],
		[3, 'cc', [0x00, 0x01, 0x02, 0x04, 0x05, 0x08, 0x09, 0x0a]],
		[4, 'note', [0x00, 0x02]],
		[4, 'cc', [0x01]],
		[5, 'note', [0x03, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0c, 0x0d]],
		[5, 'cc', [0x00, 0x01, 0x02, 0x04, 0x05, 0x08, 0x09, 0x0a]],
		[6, 'cc', [0x00, 0x01, 0x02, 0x04, 0x05, 0x08, 0x09, 0x0a]],
		...[7, 8].map((ch) => [
			ch,
			'note',
			[0x00, 0x10, 0x20, 0x30, 0x08, 0x18, 0x28, 0x38].flatMap((b) => range(b, b + 3))
		])
	].flatMap(([ch, kind, ids]) => ids.map((id) => key({ ch, kind, id })))
);

/** Every OUTPUT note row of the chart that an LED rule may drive. */
const CHART_OUTPUT = new Set(
	[2, 3]
		.flatMap((ch) => [0x05, 0x07, 0x09, 0x0a, 0x0c].map((id) => key({ ch, kind: 'note', id })))
		.concat([7, 8].flatMap((ch) => range(0, 3).map((id) => key({ ch, kind: 'note', id }))))
);

const find = (pred) => map.bindings.filter(pred);
const at = (ch, kind, id) => (b) =>
	b.source.ch === ch && b.source.kind === kind && b.source.id === id;

// --------------------------------------------------------------- tests

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(map);
	webmidi._resetMidiForTests();
});

test('nameMatch claims the Inpulse 200 only', () => {
	const re = new RegExp(map.nameMatch, 'i');
	for (const name of [
		'DJControl Inpulse 200',
		'DJControl Inpulse 200 MIDI 1',
		'DJControl Inpulse 200:DJControl Inpulse 200 MIDI 1 28:0'
	])
		assert.ok(re.test(name), name);
	for (const name of [
		'DJControl Inpulse 200 MK2',
		'DJControl Inpulse 200 mk3',
		'DJControl Inpulse 2000',
		'DJControl Inpulse 300',
		'DJControl Inpulse 500',
		'DDJ-FLX4'
	])
		assert.ok(!re.test(name), name);
});

test('every binding, hint and LED wire is in the chart', () => {
	for (const b of map.bindings)
		assert.ok(CHART_INPUT.has(key(b.source)), `binding ${key(b.source)}`);
	for (const h of map.hints) assert.ok(CHART_INPUT.has(key(h.source)), `hint ${key(h.source)}`);
	for (const l of map.leds)
		assert.ok(
			CHART_OUTPUT.has(key({ ch: l.out.ch, kind: 'note', id: l.out.note })),
			`led ${l.out.note}`
		);
});

test('play, cue, sync, fader and jog are bound per deck; crossfader global', () => {
	for (const [ch, deck] of [
		[2, 1],
		[3, 2]
	]) {
		for (const [id, type] of [
			[0x07, 'deck_play_toggle'],
			[0x06, 'deck_cue'],
			[0x05, 'deck_sync_toggle']
		]) {
			const hits = find((b) => b.action.type === type && b.action.deck === deck);
			assert.equal(hits.length, 1, `${type} deck ${deck}`);
			assert.ok(at(ch, 'note', id)(hits[0]));
		}
		const fader = find(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
		);
		assert.equal(fader.length, 1);
		assert.ok(at(ch, 'cc', 0x00)(fader[0]));
		const jogs = find((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.ok(
			jogs.length >= 1 &&
				jogs.every((b) => b.relative === true && !b.relativeCentered && b.source.ch === ch)
		);
	}
	const xf = find((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader');
	assert.equal(xf.length, 1);
	assert.ok(at(1, 'cc', 0x00)(xf[0]));
});

test('FILTER is the colour FX and the FX pads are hints only', () => {
	for (const [ch, padCh] of [
		[2, 7],
		[3, 8]
	]) {
		const f = find(at(ch, 'cc', 0x01));
		assert.equal(f.length, 1);
		assert.equal(f[0].action.target, 'filter');
		for (const id of range(0x20, 0x23)) {
			assert.equal(find(at(padCh, 'note', id)).length, 0);
			assert.ok(map.hints.some((h) => key(h.source) === key({ ch: padCh, kind: 'note', id })));
		}
	}
});

test('mixer, pitch and browse spot checks', () => {
	for (const ch of [2, 3]) {
		assert.equal(find(at(ch, 'cc', 0x02))[0].action.band, 'low');
		assert.equal(find(at(ch, 'cc', 0x04))[0].action.band, 'high');
		assert.equal(find(at(ch, 'cc', 0x05))[0].action.target, 'trim');
		const pitch = find(at(ch, 'cc', 0x08))[0];
		assert.equal(pitch.action.type, 'deck_pitch');
		assert.equal(pitch.action.lsbOffset, 0x20);
	}
	assert.ok(!map.bindings.some((b) => b.action.band === 'mid'));
	const browse = find(at(1, 'cc', 0x01))[0];
	assert.equal(browse.action.type, 'browse_encoder');
	assert.equal(browse.relative, true);
	assert.ok(!browse.relativeCentered);
});

test('hot cues on pad notes 00..03 with mirrored LEDs', () => {
	for (const [padCh, deck] of [
		[7, 1],
		[8, 2]
	]) {
		['A', 'B', 'C', 'D'].forEach((slot, i) => {
			const b = find(at(padCh, 'note', i))[0];
			assert.deepEqual(b.action, { type: 'deck_hot_cue', deck, slot });
			const led = map.leds.find(
				(l) =>
					l.trigger.kind === 'hot_cue_present' && l.trigger.deck === deck && l.trigger.slot === slot
			);
			assert.deepEqual(led.out, { ch: padCh, note: i, velocityOn: 0x7f, velocityOff: 0x00 });
		});
	}
});

test('no shift bindings; master and headphone level are hints', () => {
	assert.ok(map.bindings.every((b) => !b.shift));
	for (const id of [0x03, 0x04]) {
		assert.equal(find(at(1, 'cc', id)).length, 0);
		assert.ok(map.hints.some((h) => key(h.source) === key({ ch: 1, kind: 'cc', id })));
	}
});

test('self-registers live and is flagged hardware-unconfirmed', () => {
	assert.equal(mod.DEVICE_MAP_ENTRIES.length, 1);
	assert.equal(mod.DEVICE_MAP_ENTRIES[0].key, 'HERCULES_INPULSE_200_MAP');
	assert.equal(mod.DEVICE_MAP_ENTRIES[0].live, true);
	assert.equal(mod.HERCULES_INPULSE_200_HARDWARE_CONFIRMED, false);
});

test('no wire is duplicated across bindings and hints', () => {
	const keys = [...map.bindings, ...map.hints].map((x) => key(x.source));
	assert.equal(new Set(keys).size, keys.length);
});
