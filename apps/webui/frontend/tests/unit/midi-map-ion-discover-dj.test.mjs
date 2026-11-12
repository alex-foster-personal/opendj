// requirement: MIDI-11953
// ION Discover DJ (#10849): ION never published a MIDI chart for this unit, so no map may claim it.
// The research note docs/controller/research/ION-DISCOVER-DJ-MIDI-sources.md records the search.
//
// Regression lines (single-line format per CLAUDE.md):
//   if a map module or DEVICE_MAP_ENTRIES key names the Discover DJ before a chart or capture exists then broken
//   if any registered or exported nameMatch claims an "ION Discover DJ" port then wires were invented
//   if device-maps.json carries a map that claims a Discover DJ port then the engine binds invented wires
//   if the research note loses the official quickstart sha256 then the no-chart finding has no record

import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPO_ROOT = resolve(FRONTEND_ROOT, '../../..');
const MAPS = '/src/lib/rb/midi/maps';
const DISCOVER_PORTS = ['ION Discover DJ', 'Discover DJ', 'DISCOVER DJ', 'ION DISCOVER DJ MIDI 1'];

let vite;
let entries;
let registry;
let webmidi;
/** Every DeviceMap any map module exports, by export name. */
const exportedMaps = new Map();

const isDeviceMap = (v) =>
	v !== null &&
	typeof v === 'object' &&
	typeof v.vendor === 'string' &&
	typeof v.nameMatch === 'string' &&
	Array.isArray(v.bindings);

const tsFiles = (dir) =>
	readdirSync(resolve(FRONTEND_ROOT, dir.slice(1)))
		.filter((f) => f.endsWith('.ts'))
		.sort();

const claimsDiscoverPort = (nameMatch, port) => new RegExp(nameMatch, 'i').test(port);

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
	entries = await vite.ssrLoadModule(`${MAPS}/entries.ts`);
	registry = await vite.ssrLoadModule(`${MAPS}/index.ts`);
	for (const file of tsFiles(MAPS)) {
		const mod = await vite.ssrLoadModule(`${MAPS}/${file}`);
		for (const [name, value] of Object.entries(mod)) {
			if (isDeviceMap(value)) exportedMaps.set(name, value);
		}
	}
});

after(async () => {
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	await vite.close();
});

test('no map module or DEVICE_MAP_ENTRIES key names the Discover DJ unit', () => {
	assert.ok(exportedMaps.has('FLX4_MAP'), 'control: the directory sweep finds FLX4_MAP');
	for (const file of tsFiles(MAPS)) {
		assert.ok(!/discover/i.test(file), `${file} must not name Discover DJ before a chart exists`);
	}
	for (const { key } of entries.DEVICE_MAP_ENTRIES) {
		assert.ok(!/DISCOVER/i.test(key), `${key} must not name Discover DJ before a chart exists`);
	}
});

test('no registered map resolves an ION Discover DJ port', () => {
	for (const map of exportedMaps.values()) {
		for (const port of DISCOVER_PORTS) {
			assert.ok(
				!claimsDiscoverPort(map.nameMatch, port),
				`exported map nameMatch must not claim ${port}`
			);
		}
	}
	for (const { map } of entries.DEVICE_MAP_ENTRIES) {
		for (const port of DISCOVER_PORTS) {
			assert.ok(!claimsDiscoverPort(map.nameMatch, port), `entry nameMatch must not claim ${port}`);
		}
	}
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	for (const port of DISCOVER_PORTS) {
		assert.equal(webmidi.resolveMapForPort(port), null, port);
	}
	const flx4 = webmidi.resolveMapForPort('DDJ-FLX4');
	assert.ok(flx4 !== null && typeof flx4.vendor === 'string', 'control: FLX4 resolves');
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('the engine export carries no Discover DJ map', () => {
	const raw = readFileSync(
		resolve(FRONTEND_ROOT, '../../audio-engine/maps/device-maps.json'),
		'utf8'
	);
	const { maps } = JSON.parse(raw);
	assert.ok(Array.isArray(maps) && maps.length > 0, 'control: device-maps.json is non-empty');
	for (const map of maps) {
		for (const port of DISCOVER_PORTS) {
			assert.ok(
				!claimsDiscoverPort(map.nameMatch, port),
				`device-maps export must not claim ${port}`
			);
		}
	}
});

test('the no-chart finding is recorded with the official quickstart sha256', () => {
	const note = readFileSync(
		resolve(REPO_ROOT, 'docs/controller/research/ION-DISCOVER-DJ-MIDI-sources.md'),
		'utf8'
	);
	assert.ok(
		note.includes('7e43d472db273bac0ecc57770e589470ce28a2843957856037f29fc78f6b05dc'),
		'quickstart sha256'
	);
});
