// Denon DN-SC2000 live device map (issue #10822).
//
//   if any binding (ch, kind, id) is not in the manufacturer chart then broken
//   if nameMatch does not match the catalog model or hijacks generic USB names then broken
//   if play, cue, sync, jog, pitch, hot cues, auto loop, browse are missing on a deck then broken
//   if mixer_channel, mixer_global or fader bindings exist then broken (unit has no faders)
//   if FX chart wires are bound instead of hinted then broken
//   if hints collide with bindings or repeat then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

const CHART_NOTES = new Set([
	0x03, 0x06, 0x0c, 0x0d, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x17, 0x18, 0x19, 0x1a, 0x1d, 0x20,
	0x21, 0x22, 0x23, 0x24, 0x28, 0x29, 0x30, 0x37, 0x39, 0x42, 0x43, 0x51, 0x52, 0x53, 0x54, 0x55,
	0x56, 0x57, 0x58, 0x60, 0x64, 0x65, 0x68, 0x69, 0x6a, 0x6b
]);

const CHART_CCS = new Set([0x51, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5a, 0x5b, 0x5c, 0x5d]);

const FX_HINT_NOTES = [0x12, 0x13, 0x14, 0x15, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58];
const FX_HINT_CCS = [0x55, 0x56, 0x57, 0x58, 0x59, 0x5a, 0x5b, 0x5c, 0x5d];

const FORBIDDEN_ACTION_TYPES = new Set([
	'mixer_channel',
	'mixer_global',
	'channel_cue',
	'headphone_mix',
	'headphone_level',
	'master_cue',
	'shift_modifier'
]);

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
		resolve: { alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') }, conditions: ['browser'] }
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/denon-dn-sc2000.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

function sourcesOnChannel(ch, bindings, hints) {
	const out = new Set();
	for (const b of bindings.filter((row) => row.source.ch === ch)) {
		out.add(key(b.source));
	}
	for (const h of hints.filter((row) => row.source.ch === ch)) {
		out.add(key(h.source));
	}
	return out;
}

function findBinding(ch, kind, id) {
	return mapModule.DENON_DN_SC2000_MAP.bindings.find(
		(b) => b.source.ch === ch && b.source.kind === kind && b.source.id === id
	);
}

function chartHas(kind, id) {
	if (kind === 'pitchbend') {
		return id === 0;
	}
	if (kind === 'note') {
		return CHART_NOTES.has(id);
	}
	if (kind === 'cc') {
		return CHART_CCS.has(id);
	}
	return false;
}

test('every binding wire is in the manufacturer chart', () => {
	for (const b of mapModule.DENON_DN_SC2000_MAP.bindings) {
		assert.ok([1, 2].includes(b.source.ch), `unexpected ch ${b.source.ch}`);
		assert.ok(chartHas(b.source.kind, b.source.id), `binding ${key(b.source)} not in chart`);
	}
});

test('chart wires on ch 1 and ch 2 are fully covered by bindings plus hints', () => {
	const expectedPerCh = CHART_NOTES.size + CHART_CCS.size + 1; // + pitchbend
	for (const ch of [1, 2]) {
		const covered = sourcesOnChannel(
			ch,
			mapModule.DENON_DN_SC2000_MAP.bindings,
			mapModule.DENON_DN_SC2000_MAP.hints
		);
		assert.equal(covered.size, expectedPerCh, `ch ${ch} wire count`);
	}
});

test('nameMatch resolves the catalog model and rejects generic or sibling ports', () => {
	const catalogPath = resolve(
		FRONTEND_ROOT,
		'../../../tools/deck-diagrams/catalog/controllers.json'
	);
	const catalog = JSON.parse(readFileSync(catalogPath, 'utf8'));
	const entry = catalog.controllers.find((row) => row.id === 'denon-dn-sc2000');
	assert.ok(entry, 'catalog entry denon-dn-sc2000');
	const re = new RegExp(mapModule.DENON_DN_SC2000_MAP.nameMatch, 'i');
	assert.ok(re.test(entry.model));
	assert.ok(re.test('DN-SC2000'));
	assert.ok(re.test('Denon DJ DN-SC2000 MIDI 1'));
	assert.ok(!re.test('USB MIDI'));
	assert.ok(!re.test('USB Audio Device'));
	assert.ok(!re.test('DN-SC3900'));
	assert.ok(!re.test('SC LIVE 4'));
	assert.ok(!re.test('PRIME 4+'));
	assert.ok(!re.test('Reloop Mixtour'));

	for (const other of registry.DEVICE_MAP_REGISTRY) {
		if (other === mapModule.DENON_DN_SC2000_MAP) {
			continue;
		}
		assert.ok(
			!new RegExp(other.nameMatch, 'i').test(entry.model),
			`sibling map ${other.nameMatch} also matches catalog model`
		);
	}
});

test('map registers and resolves Denon DN SC2000 ports', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mapModule.DENON_DN_SC2000_MAP);
	assert.equal(webmidi.resolveMapForPort('Denon DN SC2000')?.nameMatch, 'DN[- ]?SC[- ]?2000');
	assert.ok(registry.DEVICE_MAP_REGISTRY.includes(mapModule.DENON_DN_SC2000_MAP));
	webmidi._resetMidiForTests();
});

test('headline transport and browse bindings exist on both decks (32 bindings total)', () => {
	assert.equal(mapModule.DENON_DN_SC2000_MAP.bindings.length, 32);
	const cueNotes = [0x17, 0x18, 0x19, 0x20, 0x21, 0x22, 0x23, 0x24];
	const slots = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
	for (const deck of [1, 2]) {
		assert.deepEqual(findBinding(deck, 'note', 0x43)?.action, {
			type: 'deck_play_toggle',
			deck
		});
		assert.deepEqual(findBinding(deck, 'note', 0x42)?.action, { type: 'deck_cue', deck });
		assert.deepEqual(findBinding(deck, 'note', 0x6b)?.action, { type: 'deck_sync_toggle', deck });
		const jog = findBinding(deck, 'cc', 0x51);
		assert.ok(jog?.relative && jog.relativeCentered);
		assert.deepEqual(jog.action, { type: 'deck_jog', deck });
		const pitch = findBinding(deck, 'pitchbend', 0);
		assert.deepEqual(pitch?.action, { type: 'deck_pitch', deck, lsbOffset: null });
		for (let i = 0; i < cueNotes.length; i += 1) {
			assert.deepEqual(findBinding(deck, 'note', cueNotes[i])?.action, {
				type: 'deck_hot_cue',
				deck,
				slot: slots[i]
			});
		}
		assert.deepEqual(findBinding(deck, 'note', 0x1d)?.action, {
			type: 'deck_auto_loop_toggle',
			deck
		});
		const browse = findBinding(deck, 'cc', 0x54);
		assert.ok(browse?.relative);
		assert.deepEqual(browse.action, { type: 'browse_encoder' });
		assert.deepEqual(findBinding(deck, 'note', 0x28)?.action, { type: 'browse_load', deck });
	}
});

test('no channel fader, crossfader or mixer bindings (hardware has none)', () => {
	for (const b of mapModule.DENON_DN_SC2000_MAP.bindings) {
		assert.ok(!FORBIDDEN_ACTION_TYPES.has(b.action.type), `forbidden ${b.action.type}`);
	}
});

test('FX and filter chart wires are hints only; map declares no leds', () => {
	assert.equal(mapModule.DENON_DN_SC2000_MAP.leds?.length ?? 0, 0);
	for (const ch of [1, 2]) {
		for (const id of FX_HINT_NOTES) {
			assert.equal(findBinding(ch, 'note', id), undefined);
		}
		for (const id of FX_HINT_CCS) {
			assert.equal(findBinding(ch, 'cc', id), undefined);
		}
	}
});

test('hints are unique and never collide with bound sources', () => {
	const bound = new Set(mapModule.DENON_DN_SC2000_MAP.bindings.map((b) => key(b.source)));
	const seen = new Set();
	for (const h of mapModule.DENON_DN_SC2000_MAP.hints) {
		const k = key(h.source);
		assert.ok(!bound.has(k), `hint collides on ${k}`);
		assert.ok(!seen.has(k), `duplicate hint ${k}`);
		seen.add(k);
		assert.ok(h.label.length > 0);
	}
});
