// requirement: MIDI-12025
// Gemini FirstMix (#10830): Gemini never published a MIDI chart for this unit, so no map may claim it.
// The research note Gemini-FirstMix-MIDI-sources.md records the search.
//
// Regression lines (single-line format per CLAUDE.md):
//   if a map module or DEVICE_MAP_ENTRIES key names the FirstMix before a chart or capture exists then broken
//   if any registered map's nameMatch claims a Gemini FirstMix port then invented wires drive the unit
//   if the live registry the engine export is built from claims a FirstMix port then the engine binds invented wires

import assert from 'node:assert/strict';
import { readdirSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const MAPS = '/src/lib/rb/midi/maps';
const FIRSTMIX_PORTS = [
	'Gemini FirstMix',
	'FirstMix',
	'FIRSTMIX',
	'GEMINI FIRSTMIX MIDI 1',
	'FirstMix Pro',
	'FirstMix I/O'
];

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

const claimsFirstmixPort = (nameMatch, port) => new RegExp(nameMatch, 'i').test(port);

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

test('no map module or DEVICE_MAP_ENTRIES key names the FirstMix unit', () => {
	assert.ok(exportedMaps.has('FLX4_MAP'), 'control: the directory sweep finds FLX4_MAP');
	for (const file of tsFiles(MAPS)) {
		assert.ok(!/firstmix/i.test(file), `${file} must not name FirstMix before a chart exists`);
	}
	for (const { key } of entries.DEVICE_MAP_ENTRIES) {
		assert.ok(!/FIRSTMIX/i.test(key), `${key} must not name FirstMix before a chart exists`);
	}
});

test('no registered map resolves a Gemini FirstMix port', () => {
	for (const map of exportedMaps.values()) {
		for (const port of FIRSTMIX_PORTS) {
			assert.ok(
				!claimsFirstmixPort(map.nameMatch, port),
				`exported map nameMatch must not claim ${port}`
			);
		}
	}
	for (const { map } of entries.DEVICE_MAP_ENTRIES) {
		for (const port of FIRSTMIX_PORTS) {
			assert.ok(!claimsFirstmixPort(map.nameMatch, port), `entry nameMatch must not claim ${port}`);
		}
	}
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	for (const port of FIRSTMIX_PORTS) {
		assert.equal(webmidi.resolveMapForPort(port), null, port);
	}
	const flx4 = webmidi.resolveMapForPort('DDJ-FLX4');
	assert.ok(flx4 !== null && typeof flx4.vendor === 'string', 'control: FLX4 resolves');
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('DEVICE_MAP_REGISTRY carries no FirstMix map', () => {
	const maps = registry.DEVICE_MAP_REGISTRY;
	assert.ok(Array.isArray(maps) && maps.length > 0, 'control: DEVICE_MAP_REGISTRY is non-empty');
	for (const map of maps) {
		for (const port of FIRSTMIX_PORTS) {
			assert.ok(!claimsFirstmixPort(map.nameMatch, port), `registry export must not claim ${port}`);
		}
	}
});
