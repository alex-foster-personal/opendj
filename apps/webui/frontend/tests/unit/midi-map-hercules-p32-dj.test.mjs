// requirement: MIDI-11959
// Unit tests for the Hercules P32 DJ device map (src/lib/rb/midi/maps/hercules-p32-dj.ts).
//
// Regression lines (single-line format per CLAUDE.md):
//   if any binding, hint or LED (channel, kind, id) is not a row of the Hercules chart then broken
//   if play, cue, sync, a channel fader, the crossfader or browse is unbound then broken
//   if a deck_jog or deck_pitch binding appears (the P32 DJ has no jog wheel or pitch fader) then broken
//   if an FX control is bound or dropped instead of hinted then broken
//   if hot cue pads 1-8 are not deck_hot_cue A-H then broken
//   if hints repeat or collide with bound sources then broken
//   if a Hercules P32 DJ MIDI 1 port does not resolve to HERCULES_P32_DJ_MAP then broken

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
let registry;

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-p32-dj.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

// Transcription of the Hercules P32 DJ MIDI Command List Rev1.0: GLOBAL MIXER (ch 1) and
// GLOBAL MIXER + SHIFT (ch 4) on p.2; DECK COMMAND LIST p.3-5 (decks A/B ch 2/3, +SHIFT ch 5/6).
// No jog or pitch row exists anywhere in the chart.
const CHART = new Set(['1:cc:1', '1:cc:2', '4:cc:2', '4:note:1']);
for (let id = 1; id <= 5; id++) CHART.add(`1:note:${id}`);
for (const ch of [2, 3, 5, 6]) {
	for (let id = 0x01; id <= 0x0a; id++) CHART.add(`${ch}:cc:${id}`);
	for (let id = 0x01; id <= 0x10; id++) CHART.add(`${ch}:note:${id}`);
	for (let id = 0x24; id <= 0x63; id++) CHART.add(`${ch}:note:${id}`);
}
// MIDI OUTPUT rows p.5-7: LED notes on the deck channels.
const LED_OUT = new Set();
for (const ch of [2, 3]) {
	for (const id of [0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x10])
		LED_OUT.add(`${ch}:${id}`);
	for (let id = 0x24; id <= 0x63; id++) LED_OUT.add(`${ch}:${id}`);
}

const map = () => mapModule.HERCULES_P32_DJ_MAP;
function theBinding(pred) {
	const hits = map().bindings.filter(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

test('every binding and hint source is a row of the manufacturer chart', () => {
	for (const b of map().bindings) assert.ok(CHART.has(key(b.source)), `binding ${key(b.source)}`);
	for (const h of map().hints) assert.ok(CHART.has(key(h.source)), `hint ${key(h.source)}`);
});

test('play, cue, sync, channel faders, crossfader and browse are bound to the chart wires', () => {
	for (const deck of [1, 2]) {
		const ch = deck + 1;
		const at = (kind, id) => (b) =>
			b.source.ch === ch && b.source.kind === kind && b.source.id === id;
		assert.equal(theBinding(at('note', 0x0a)).action.type, 'deck_play_toggle');
		assert.equal(theBinding(at('note', 0x09)).action.type, 'deck_cue');
		assert.equal(theBinding(at('note', 0x08)).action.type, 'deck_sync_toggle');
		assert.equal(theBinding(at('note', 0x0f)).action.type, 'browse_load');
		const fader = theBinding(at('cc', 0x01)).action;
		assert.deepEqual([fader.type, fader.deck, fader.target], ['mixer_channel', deck, 'fader']);
	}
	const xf = theBinding((b) => b.action.type === 'mixer_global');
	assert.deepEqual([key(xf.source), xf.action.target], ['1:cc:1', 'crossfader']);
	const browse = theBinding((b) => b.action.type === 'browse_encoder');
	assert.equal(key(browse.source), '1:cc:2');
	assert.equal(browse.relative, true);
});

test('no jog or pitch binding: the jog acceptance item does not apply to this hardware', () => {
	for (const b of map().bindings) assert.ok(!['deck_jog', 'deck_pitch'].includes(b.action.type));
});

test('FX controls are hinted, not bound, until the MIDI contract has an FX action', () => {
	// Pins today's contract (no FX MidiAction); a future FX action PR must flip this test.
	const bound = new Set(map().bindings.map((b) => key(b.source)));
	const hinted = new Set(map().hints.map((h) => key(h.source)));
	for (const ch of [2, 3]) {
		for (let id = 0x05; id <= 0x09; id++) {
			assert.ok(hinted.has(`${ch}:cc:${id}`) && !bound.has(`${ch}:cc:${id}`), `${ch}:cc:${id}`);
		}
		for (let id = 0x02; id <= 0x06; id++) {
			assert.ok(
				hinted.has(`${ch}:note:${id}`) && !bound.has(`${ch}:note:${id}`),
				`${ch}:note:${id}`
			);
		}
	}
});

test('HOTCUE pads 1-8 are deck_hot_cue slots A-H on both decks', () => {
	for (const deck of [1, 2]) {
		[...'ABCDEFGH'].forEach((slot, i) => {
			const b = theBinding(
				(x) => x.source.ch === deck + 1 && x.source.kind === 'note' && x.source.id === 0x54 + i
			);
			assert.deepEqual(b.action, { type: 'deck_hot_cue', deck, slot });
		});
	}
});

test('hints never repeat a source and never collide with a bound source', () => {
	const bound = new Set(map().bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of map().hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint collides with binding ${k}`);
		assert.ok(!seen.has(k), `hint repeats ${k}`);
		seen.add(k);
	}
});

test('nameMatch matches the P32 DJ port names and resolves through the registry', () => {
	const re = new RegExp(map().nameMatch, 'i');
	for (const name of ['Hercules P32 DJ', 'Hercules P32 DJ MIDI 1', 'HERCULES P32 DJ'])
		assert.ok(re.test(name), name);
	for (const name of [
		'DJControl Inpulse 300',
		'Hercules DJControl Compact',
		'DDJ-FLX4',
		'Reloop Mixtour Pro'
	])
		assert.ok(!re.test(name), name);
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	assert.equal(webmidi.resolveMapForPort('Hercules P32 DJ MIDI 1'), map());
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('the map self-registers as one live entry', () => {
	assert.deepEqual(
		mapModule.DEVICE_MAP_ENTRIES.map((e) => [e.key, e.live]),
		[['HERCULES_P32_DJ_MAP', true]]
	);
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(map()));
});

test('LED outs are chart output rows with 0x7F on and 0x00 off', () => {
	for (const rule of map().leds) {
		assert.ok(LED_OUT.has(`${rule.out.ch}:${rule.out.note}`), `${rule.out.ch}:${rule.out.note}`);
		assert.equal(rule.out.velocityOn, 0x7f);
		assert.equal(rule.out.velocityOff, 0x00);
	}
	for (const deck of [1, 2]) {
		const note = (kind) =>
			map().leds.filter((r) => r.trigger.kind === kind && r.trigger.deck === deck);
		assert.equal(note('deck_playing')[0].out.note, 0x0a);
		assert.equal(note('beat_sync_enabled')[0].out.note, 0x08);
		assert.equal(note('channel_cue_enabled')[0].out.note, 0x10);
		assert.deepEqual(
			note('hot_cue_present').map((r) => r.out.note),
			[0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5a, 0x5b]
		);
	}
});
