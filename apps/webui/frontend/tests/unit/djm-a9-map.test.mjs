// requirement: SKIN-10484
// Unit tests for the Pioneer DJ DJM-A9 P0 device map (src/lib/rb/midi/maps/djm-a9.ts),
// its entry in the maps/index.ts registry, and the practice replica's join against it.
//
// Every expected wire number is read off the official "DJM-A9 MIDI Message List" PDF
// (E, 1.0), [PDF] Part No. rows quoted per assertion.
//
// Regression lines (single-line format per CLAUDE.md):
//   if registerDeviceMap(DJM_A9_MAP) throws then broken
//   if nameMatch matches DJM-900NXS2, DJM-V10, DJM-S9 or XDJ-AZ then broken
//   if a channel's TRIM/HI/MID/LOW/COLOR/CH FADER are not the sheet's CCs on the one MIDI channel ([PDF] C2-C6, C8) then broken
//   if CH2 CH FADER is not CC 0x12 and CH2 CUE B not note 0x11 (the sheet-anomaly reading) then broken
//   if CUE A per channel is not channel_cue on notes 0x0A-0x0D ([PDF] C7) then broken
//   if CROSSFADER, MASTER LEVEL, HEADPHONES A MIX/LEVEL, MASTER CUE A are not bound ([PDF] C10, R1, L21, L22, R5) then broken
//   if the map binds any deck transport, pad, browse or shift action (a mixer has none) then broken
//   if the map carries LED rules (the DJM-A9 accepts no MIDI input) then broken
//   if hints collide with bound sources or repeat then broken
//   if any panel control (midi or shift_midi) is neither bound nor hinted then broken
//   if the bound count over the panel is not exactly 33, inert not exactly 68 then broken
//   if the registry does not carry DJM_A9_MAP then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/djm-a9/panel.json');

let vite;
let webmidi;
let mapModule;
let registry;
let bindings;
const layout = JSON.parse(readFileSync(PANEL, 'utf8'));

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/djm-a9.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
	bindings = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-bindings.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function findBindings(pred) {
	return mapModule.DJM_A9_MAP.bindings.filter(pred);
}

function theBinding(pred) {
	const hits = findBindings(pred);
	assert.equal(hits.length, 1, `expected exactly 1 binding, got ${hits.length}`);
	return hits[0];
}

const CHANNELS = [1, 2, 3, 4];

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.DJM_A9_MAP);
	webmidi._resetMidiForTests();
});

test('nameMatch covers DJM-A9 port names only', () => {
	const re = new RegExp(mapModule.DJM_A9_MAP.nameMatch, 'i');
	assert.ok(re.test('DJM-A9'));
	assert.ok(re.test('Pioneer DJ DJM-A9 MIDI 1'));
	assert.ok(!re.test('DJM-900NXS2'));
	assert.ok(!re.test('DJM-V10'));
	assert.ok(!re.test('DJM-S9'));
	assert.ok(!re.test('XDJ-AZ'));
	assert.ok(!re.test('DDJ-FLX4'));
});

test('every binding sends on the one UTILITY MIDI channel ([PDF] *1), default 1', () => {
	assert.equal(mapModule.A9_MIDI_CH, 1);
	for (const b of mapModule.DJM_A9_MAP.bindings) assert.equal(b.source.ch, mapModule.A9_MIDI_CH);
	for (const h of mapModule.DJM_A9_MAP.hints) assert.equal(h.source.ch, mapModule.A9_MIDI_CH);
});

test('per-channel strip CCs and CUE A ([PDF] C2-C8), CH2 read past the sheet anomaly', () => {
	const expect = {
		1: { trim: 0x01, high: 0x02, mid: 0x03, low: 0x04, filter: 0x05, fader: 0x11, cue: 0x0a },
		2: { trim: 0x06, high: 0x07, mid: 0x08, low: 0x09, filter: 0x0a, fader: 0x12, cue: 0x0b },
		3: { trim: 0x0c, high: 0x0e, mid: 0x0f, low: 0x15, filter: 0x16, fader: 0x13, cue: 0x0c },
		4: { trim: 0x50, high: 0x51, mid: 0x5c, low: 0x52, filter: 0x53, fader: 0x14, cue: 0x0d }
	};
	for (const n of CHANNELS) {
		const e = expect[n];
		const cases = [
			[(a) => a.target === 'trim', e.trim],
			[(a) => a.target === 'eq' && a.band === 'high', e.high],
			[(a) => a.target === 'eq' && a.band === 'mid', e.mid],
			[(a) => a.target === 'eq' && a.band === 'low', e.low],
			[(a) => a.target === 'filter', e.filter],
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
	// The anomaly reading: CH2 CUE B is a hint on note 0x11, never a CC binding on 0x11.
	const cueB2 = mapModule.DJM_A9_MAP.hints.filter((h) => key(h.source) === '1:note:17');
	assert.equal(cueB2.length, 1);
	assert.match(cueB2[0].label, /CUE B \(channel 2\)/);
});

test('global mixer rows ([PDF] C10, R1, L21, L22, R5)', () => {
	assert.deepEqual(
		theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader').source,
		{ ch: 1, kind: 'cc', id: 0x0b }
	);
	assert.deepEqual(
		theBinding((b) => b.action.type === 'mixer_global' && b.action.target === 'master').source,
		{ ch: 1, kind: 'cc', id: 0x18 }
	);
	assert.deepEqual(theBinding((b) => b.action.type === 'headphone_mix').source, {
		ch: 1,
		kind: 'cc',
		id: 0x1b
	});
	assert.deepEqual(theBinding((b) => b.action.type === 'headphone_level').source, {
		ch: 1,
		kind: 'cc',
		id: 0x1a
	});
	const masterCue = theBinding((b) => b.action.type === 'master_cue');
	assert.deepEqual(masterCue.source, { ch: 1, kind: 'note', id: 0x0e });
	assert.equal(masterCue.action.mode, 'latch');
	assert.equal(mapModule.DJM_A9_MAP.bindings.length, 4 * 7 + 5);
});

test('a mixer map: no transport, pad, browse, pitch or shift bindings, and no LED rules', () => {
	const kinds = new Set(mapModule.DJM_A9_MAP.bindings.map((b) => b.action.type));
	assert.deepEqual([...kinds].sort(), [
		'channel_cue',
		'headphone_level',
		'headphone_mix',
		'master_cue',
		'mixer_channel',
		'mixer_global'
	]);
	assert.equal(mapModule.DJM_A9_MAP.leds, undefined);
});

test('hints are unique and never collide with bound sources', () => {
	const bound = new Set(mapModule.DJM_A9_MAP.bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of mapModule.DJM_A9_MAP.hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint ${h.label} collides with a binding on ${k}`);
		assert.ok(!seen.has(k), `hint ${h.label} repeats ${k}`);
		seen.add(k);
		assert.ok(h.label.length > 0);
	}
	// Controls the replica draws inert must be hinted: TALK OVER, SPACE, X-PAD slide,
	// ON/OFF (shared CC 0x72), EFFECT SELECT DELAY, CH SELECT 1, TIME MSB and LSB, CH1 INPUT USB.
	for (const k of [
		'1:note:97',
		'1:note:85',
		'1:cc:116',
		'1:cc:114',
		'1:cc:42',
		'1:note:1',
		'1:cc:13',
		'1:cc:45',
		'1:note:74'
	]) {
		assert.ok(seen.has(k), `expected a hint on ${k}`);
	}
	assert.equal(mapModule.DJM_A9_MAP.hints.length, 117);
});

test('the practice panel joins to DJM_A9_MAP: 33 bound, 68 inert, nothing unresolved', () => {
	assert.equal(layout.device_map, 'DJM_A9_MAP');
	const resolved = bindings.resolveLayout(mapModule.DJM_A9_MAP, layout);
	assert.equal(resolved.length, 101);
	const bound = resolved.filter((r) => r.kind === 'bound');
	assert.equal(bound.length, 33, `bound ${bound.length}`);
	assert.equal(resolved.length - bound.length, 68);
	const byId = (id) => resolved.find((r) => r.control.id === id);
	assert.deepEqual(byId('ch3-fader').binding.action, {
		type: 'mixer_channel',
		deck: 3,
		target: 'fader'
	});
	assert.deepEqual(byId('ch4-color').binding.action, {
		type: 'mixer_channel',
		deck: 4,
		target: 'filter'
	});
	assert.deepEqual(byId('ch2-cue-a').binding.action, { type: 'channel_cue', deck: 2 });
	assert.equal(byId('crossfader').binding.action.target, 'crossfader');
	assert.equal(byId('phones-a-mix').binding.action.type, 'headphone_mix');
	for (const id of ['fx-on-off', 'cfx-space', 'x-pad', 'ch1-input', 'fx-select', 'talkover']) {
		const r = byId(id);
		assert.equal(r.kind, 'inert', `${id} should be inert`);
		assert.match(bindings.controlTitle(r), /not implemented - see PARITY-TODO/, id);
	}
	// No SHIFT layer on a mixer: every control sends the same wire with SHIFT held.
	assert.ok(layout.controls.every((c) => c.shift_midi === null));
	assert.deepEqual(
		bindings.resolveControl(mapModule.DJM_A9_MAP, byId('ch1-trim').control, true).binding.action,
		{ type: 'mixer_channel', deck: 1, target: 'trim' }
	);
	// Negative control: a wire the sheet never lists throws.
	const bogus = { ...layout.controls[0], id: 'bogus', midi: { ch: 1, kind: 'note', id: 0x7e } };
	assert.throws(
		() => bindings.resolveControl(mapModule.DJM_A9_MAP, bogus),
		/neither binds nor hints/
	);
});

test('the registry carries DJM_A9_MAP', () => {
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(mapModule.DJM_A9_MAP));
	assert.equal(registry.DEVICE_MAP_REGISTRY.filter((m) => m.nameMatch === 'DJM-A9').length, 1);
});
