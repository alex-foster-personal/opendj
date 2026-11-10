// requirement: MIDI-11945
// Numark publishes no MIDI chart for the Mixtrack Platinum FX (issue #10793), so no map ships until a capture.
//
//   if a numark-mixtrack-platinum-fx map file or a Platinum FX DEVICE_MAP_ENTRIES entry exists then wires were invented
//   if any registered or exported map's nameMatch claims a Mixtrack Platinum FX port name then another map's wires drive it
//   if device-maps.json carries a map matching a Mixtrack Platinum FX port name then the engine fires invented wires
//   if the research note stops recording that no MIDI message list is published then the no-map ruling has no record

import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const MAPS_DIR = resolve(FRONTEND_ROOT, 'src/lib/rb/midi/maps');
const RESEARCH_NOTE = resolve(
	FRONTEND_ROOT,
	'../../../docs/controller/research/Numark-Mixtrack-Platinum-FX-sources.md'
);
const DEVICE_MAPS_JSON = resolve(FRONTEND_ROOT, '../../audio-engine/maps/device-maps.json');

const PORT_NAMES = [
	'MixTrack Platinum FX',
	'Numark MixTrack Platinum FX',
	'Mixtrack Platinum FX MIDI 1'
];

// Replaced when a hardware-capture follow-up ships a chart-backed map for this device.

let vite;
let entries;
let registry;
let webmidi;

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
	entries = await vite.ssrLoadModule('/src/lib/rb/midi/maps/entries.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

test('no Platinum FX map module exists', () => {
	const mapPath = resolve(MAPS_DIR, 'numark-mixtrack-platinum-fx.ts');
	assert.equal(
		existsSync(mapPath),
		false,
		'numark-mixtrack-platinum-fx.ts must not exist without a manufacturer chart'
	);
	for (const file of readdirSync(MAPS_DIR)) {
		assert.doesNotMatch(
			file,
			/platinum/i,
			`maps directory must not contain a Platinum map file: ${file}`
		);
	}
	for (const { key } of entries.DEVICE_MAP_ENTRIES) {
		assert.doesNotMatch(key, /PLATINUM/i, `${key} must not register a Platinum FX map`);
	}
});

test('no registered map claims a Platinum FX port', () => {
	for (const { key, map } of entries.DEVICE_MAP_ENTRIES) {
		const pattern = new RegExp(map.nameMatch, 'i');
		for (const name of PORT_NAMES) {
			assert.equal(
				pattern.test(name),
				false,
				`${key} nameMatch must not match Platinum FX port "${name}"`
			);
		}
	}
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	for (const name of PORT_NAMES) {
		assert.equal(webmidi.resolveMapForPort(name), null, `port "${name}" must stay unmapped`);
	}
});

test('the engine export carries no Platinum FX map', () => {
	const { maps } = JSON.parse(readFileSync(DEVICE_MAPS_JSON, 'utf8'));
	for (const entry of maps) {
		if (entry.vendor === 'Numark') {
			assert.fail(`device-maps.json must not list Numark vendor entry: ${entry.nameMatch}`);
		}
		const pattern = new RegExp(entry.nameMatch, 'i');
		for (const name of PORT_NAMES) {
			assert.equal(
				pattern.test(name),
				false,
				`device-maps nameMatch must not match Platinum FX port "${name}"`
			);
		}
	}
});

test('the research note records that no MIDI list is published', () => {
	const text = readFileSync(RESEARCH_NOTE, 'utf8');
	assert.match(text, /None published/);
	assert.match(text, /6eac973b498733d77b7707a97d93e632aa1b4fc3bb83ee1caf4922eea229e4d3/);
	assert.match(text, /#10793/);
});
