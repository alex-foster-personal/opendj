// requirement: MIDI-11989
// Unit tests for the Dj-Tech DJM-101 P0 device map (src/lib/rb/midi/maps/dj-tech-djm-101.ts)
// and its entry in the maps/index.ts registry. No practice replica yet: do not
// read panel.json or call resolveLayout.
//
// Every expected wire number is read off the DJ-Tech DJM-101 User Manual MIDI
// hex chart (ManualsLib pages 24-28), cited as [MANUAL] per assertion.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(DJM_101_MAP) throws then broken
//   if nameMatch matches Mix-101, Mixer One, Kontrol One, DJM-A9, CDJ-101, VTT-101 or DDJ-FLX4 then broken
//   if a channel's GAIN/HI/MID/LOW/fader/PFL are not the chart's CC/Note on MIDI channel 1 ([MANUAL] pages 24-28) then broken
//   if CROSSFADER, MASTER VOLUME, MONITOR SOURCE or MONITOR LEVEL are not bound ([MANUAL] pages 24-28) then broken
//   if the map binds any deck transport, pad, browse, jog, FX or shift action (this mixer has none) then broken
//   if PFL lamps are not LED rules on notes 0x21 / 0x22 ([MANUAL] MIDI-OUT) then broken
//   if hints collide with bound sources or repeat then broken
//   if the registry does not carry DJM_101_MAP then broken

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/dj-tech-djm-101.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return mapModule.DJM_101_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const CHANNELS = [1, 2];

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.DJM_101_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers DJM-101 port names only', () => {
	const re = new RegExp(mapModule.DJM_101_MAP.nameMatch, 'i');
	assert.ok(re.test('DJM-101'));
	assert.ok(re.test('DJ-Tech DJM-101 MIDI 1'));
	assert.ok(!re.test('Mix-101'));
	assert.ok(!re.test('Mixer One'));
	assert.ok(!re.test('Kontrol One'));
	assert.ok(!re.test('DJM-A9'));
	assert.ok(!re.test('CDJ-101'));
	assert.ok(!re.test('VTT-101'));
	assert.ok(!re.test('DDJ-FLX4'));
});

test('every binding, hint and LED uses the one MIDIOUT channel ([MANUAL] page 4), default 1', () => {
	assert.equal(mapModule.DJM_101_MIDI_CH, 1);
	for (const b of mapModule.DJM_101_MAP.bindings) {
		assert.equal(b.source.ch, mapModule.DJM_101_MIDI_CH);
	}
	for (const h of mapModule.DJM_101_MAP.hints) {
		assert.equal(h.source.ch, mapModule.DJM_101_MIDI_CH);
	}
	for (const led of mapModule.DJM_101_MAP.leds) {
		assert.equal(led.out.ch, mapModule.DJM_101_MIDI_CH);
	}
});

test('per-channel strip CCs and PFL notes ([MANUAL] pages 24-28)', () => {
	const expect = {
		1: { trim: 0x48, high: 0x4a, mid: 0x4e, low: 0x4c, fader: 0x41, cue: 0x21 },
		2: { trim: 0x4f, high: 0x4d, mid: 0x4b, low: 0x49, fader: 0x42, cue: 0x22 }
	};
	for (const n of CHANNELS) {
		const e = expect[n];
		const cases = [
			[(a) => a.target === 'trim', e.trim],
			[(a) => a.target === 'eq' && a.band === 'high', e.high],
			[(a) => a.target === 'eq' && a.band === 'mid', e.mid],
			[(a) => a.target === 'eq' && a.band === 'low', e.low],
			[(a) => a.target === 'fader', e.fader]
		];
		for (const [pred, cc] of cases) {
			const b = theBinding(
				(x) => x.action.type === 'mixer_channel' && x.action.deck === n && pred(x.action)
			);
			assert.deepEqual(b.source, { ch: 1, kind: 'cc', id: cc }, `channel ${n}`);
		}
		const cue = theBinding((x) => x.action.type === 'channel_cue' && x.action.deck === n);
		assert.deepEqual(cue.source, { ch: 1, kind: 'note', id: e.cue });
	}
});

test('global mixer rows ([MANUAL] pages 24-28 CROSSFADER, MASTER, MONITOR SOURCE, MONITOR LEVEL)', () => {
	assert.deepEqual(
		theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader').source,
		{ ch: 1, kind: 'cc', id: 0x40 }
	);
	assert.deepEqual(
		theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'master').source,
		{ ch: 1, kind: 'cc', id: 0x43 }
	);
	assert.deepEqual(theBinding((b) => b.action.type === 'headphone_mix').source, {
		ch: 1,
		kind: 'cc',
		id: 0x44
	});
	assert.deepEqual(theBinding((b) => b.action.type === 'headphone_level').source, {
		ch: 1,
		kind: 'cc',
		id: 0x45
	});
	assert.equal(mapModule.DJM_101_MAP.bindings.length, 2 * 6 + 4);
});

test('a mixer map: only mixer, cue and headphone actions, no transport or FX', () => {
	const kinds = new Set(mapModule.DJM_101_MAP.bindings.map((b) => b.action.type));
	assert.deepEqual([...kinds].sort(), [
		'channel_cue',
		'headphone_level',
		'headphone_mix',
		'mixer_channel',
		'mixer_global'
	]);
});

test('PFL lamps are MIDI-OUT LED rules on the chart notes ([MANUAL] pages 24-28)', () => {
	const leds = mapModule.DJM_101_MAP.leds;
	assert.equal(leds.length, 2);
	for (const n of CHANNELS) {
		const led = leds.find((r) => r.trigger.kind === 'channel_cue_enabled' && r.trigger.deck === n);
		assert.ok(led, `PFL LED for channel ${n}`);
		assert.deepEqual(led.out, {
			ch: 1,
			note: n === 1 ? 0x21 : 0x22,
			velocityOn: 0x7f,
			velocityOff: 0x00
		});
	}
});

test('hints are unique and never collide with bound sources', () => {
	const bound = new Set(mapModule.DJM_101_MAP.bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of mapModule.DJM_101_MAP.hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint ${h.label} collides with a binding on ${k}`);
		assert.ok(!seen.has(k), `hint ${h.label} repeats ${k}`);
		seen.add(k);
		assert.ok(h.label.length > 0);
	}
	for (const k of ['1:note:35', '1:note:36', '1:cc:80', '1:cc:81', '1:cc:82', '1:cc:83']) {
		assert.ok(seen.has(k), `expected a hint on ${k}`);
	}
	assert.equal(mapModule.DJM_101_MAP.hints.length, 6);
	const meterSelect = mapModule.DJM_101_MAP.hints.filter((h) =>
		h.label.startsWith('Master / PFL meter select')
	);
	assert.equal(meterSelect.length, 2);
});

test('the registry carries DJM_101_MAP', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(mapModule.DJM_101_MAP));
	assert.equal(registry.DEVICE_MAP_REGISTRY.filter((m) => m.nameMatch === 'DJM-101').length, 1);
	assert.deepEqual(
		mapModule.DEVICE_MAP_ENTRIES.map((e) => e.key),
		['DJM_101_MAP']
	);
	assert.equal(mapModule.DEVICE_MAP_ENTRIES[0].live, true);
});
