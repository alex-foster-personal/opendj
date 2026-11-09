// requirement: SKINREG-10586
// Self-registering device maps (#10560): a new controller or replica adds its own files and
// edits no shared list. Every assertion checks the shape of what was discovered, never a
// hard-coded list or count, so adding a map never edits this file.
//
// Regression lines (single-line format per CLAUDE.md):
//   if a map module exports a DeviceMap that no DEVICE_MAP_ENTRIES entry registers under its export name then broken
//   if DEVICE_MAP_REGISTRY is not exactly the live entries in key order then broken
//   if a live map has no bindings, or a hints-only map has no hints or no practice fragment then broken
//   if a plain-literal nameMatch resolves to another registered map (first match wins) then broken
//   if a practice fragment registers a map that is not self-registered, or under another name, then broken
//   if a panel names a device_map missing from PRACTICE_DEVICE_MAPS, or a panel control fails to resolve then broken
//   if two modules register one key, or two fragments one device_map, and nothing throws then broken
//   if the collected entries depend on module order then broken
//   if the esbuild test bundler (vite-eager-glob-plugin.mjs) discovers different maps than Vite then broken

import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const MAPS = '/src/lib/rb/midi/maps';
const FRAGMENTS = '/src/lib/components/rb/practice/devices';
const PANELS_DIR = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices');

let vite;
let entries;
let registry;
let devices;
let bindings;
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
	devices = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-devices.ts');
	bindings = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-bindings.ts');
	for (const file of tsFiles(MAPS)) {
		const mod = await vite.ssrLoadModule(`${MAPS}/${file}`);
		for (const [name, value] of Object.entries(mod)) {
			if (isDeviceMap(value)) exportedMaps.set(name, value);
		}
	}
});

after(async () => {
	await vite.close();
});

test('every map file in the maps directory is registered under its export name', () => {
	assert.ok(exportedMaps.has('FLX4_MAP'), 'control: the directory sweep finds FLX4_MAP');
	const byKey = new Map(entries.DEVICE_MAP_ENTRIES.map((e) => [e.key, e]));
	for (const [name, map] of exportedMaps) {
		assert.equal(byKey.get(name)?.map, map, `${name} is exported but not in DEVICE_MAP_ENTRIES`);
	}
	for (const e of entries.DEVICE_MAP_ENTRIES) {
		assert.equal(exportedMaps.get(e.key), e.map, `${e.key} is not the export of that name`);
	}
});

test('each entry has the registry shape', () => {
	for (const { key, map, live } of entries.DEVICE_MAP_ENTRIES) {
		assert.match(key, /^[A-Z][A-Z0-9_]*_MAP$/);
		assert.ok(map.vendor.length > 0, key);
		assert.doesNotThrow(() => new RegExp(map.nameMatch, 'i'), key);
		assert.equal(typeof live, 'boolean', key);
		if (live) {
			assert.ok(map.bindings.length > 0, `${key} is live with no bindings`);
		} else {
			assert.ok((map.hints ?? []).length > 0, `${key} is hints-only with no hints`);
			assert.equal(
				devices.PRACTICE_DEVICE_MAPS[key],
				map,
				`${key} is hints-only with no practice fragment`
			);
		}
	}
});

test('the live registry is the live entries in key order, and each plain name resolves to its own map', () => {
	const live = entries.DEVICE_MAP_ENTRIES.filter((e) => e.live);
	assert.deepEqual(
		registry.DEVICE_MAP_REGISTRY,
		live.map((e) => e.map)
	);
	const keys = live.map((e) => e.key);
	assert.deepEqual(keys, [...keys].sort());
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
	registry.registerAllDeviceMaps();
	const plain = registry.DEVICE_MAP_REGISTRY.filter((m) => /^[A-Za-z0-9 -]+$/.test(m.nameMatch));
	assert.ok(plain.length > 0, 'control: some map has a plain nameMatch');
	for (const map of plain)
		assert.equal(webmidi.resolveMapForPort(map.nameMatch), map, map.nameMatch);
	webmidi._resetMidiForTests();
	registry._resetMapsRegistryForTests();
});

test('every practice fragment registers self-registered maps under their own names', async () => {
	const files = tsFiles(FRAGMENTS);
	assert.ok(files.length > 0, 'control: the fragment sweep finds files');
	let seen = 0;
	for (const file of files) {
		const { PRACTICE_MAPS } = await vite.ssrLoadModule(`${FRAGMENTS}/${file}`);
		assert.ok(Object.keys(PRACTICE_MAPS).length > 0, `${file} registers nothing`);
		for (const [name, map] of Object.entries(PRACTICE_MAPS)) {
			assert.equal(exportedMaps.get(name), map, `${file}: ${name} is not the self-registered map`);
			assert.equal(devices.PRACTICE_DEVICE_MAPS[name], map, `${file}: ${name}`);
			seen += 1;
		}
	}
	assert.equal(Object.keys(devices.PRACTICE_DEVICE_MAPS).length, seen);
});

test('every committed panel resolves against its practice map on both layers', () => {
	const panels = readdirSync(PANELS_DIR)
		.map((d) => resolve(PANELS_DIR, d, 'panel.json'))
		.filter((p) => existsSync(p));
	assert.ok(panels.length > 0, 'control: the panel sweep finds panels');
	const named = new Set();
	for (const path of panels) {
		const layout = JSON.parse(readFileSync(path, 'utf8'));
		const map = devices.practiceMapFor(layout);
		named.add(layout.device_map);
		assert.doesNotThrow(() => bindings.resolveLayout(map, layout), path);
		for (const control of layout.controls) {
			if (control.shift_midi === null || control.shift_midi === undefined) continue;
			assert.doesNotThrow(
				() => bindings.resolveControl(map, control, true),
				`${path} ${control.id}`
			);
		}
	}
	for (const name of Object.keys(devices.PRACTICE_DEVICE_MAPS)) {
		assert.ok(named.has(name), `${name} has a practice fragment but no panel names it`);
	}
});

test('the collectors fail fast on a second registration and ignore module order', () => {
	const [a, b] = entries.DEVICE_MAP_ENTRIES;
	const one = {
		'./a.ts': { DEVICE_MAP_ENTRIES: [b] },
		'./b.ts': { DEVICE_MAP_ENTRIES: [a] },
		'./h.ts': {}
	};
	const two = { './b.ts': one['./b.ts'], './a.ts': one['./a.ts'] };
	assert.deepEqual(entries.collectDeviceMapEntries(one), entries.collectDeviceMapEntries(two));
	assert.throws(
		() =>
			entries.collectDeviceMapEntries({
				'./a.ts': { DEVICE_MAP_ENTRIES: [a] },
				'./c.ts': { DEVICE_MAP_ENTRIES: [a] }
			}),
		/register/
	);
	const frag = { PRACTICE_MAPS: { [a.key]: a.map } };
	assert.throws(
		() => devices.mergePracticeFragments({ './x.ts': frag, './y.ts': frag }),
		/second time/
	);
});

test('the esbuild test bundler discovers the same maps and fragments as Vite', async () => {
	const names = (maps) => maps.map((m) => m.nameMatch);
	const bundledRegistry = await loadTypeScriptModule('src/lib/rb/midi/maps/index.ts');
	assert.deepEqual(names(bundledRegistry.DEVICE_MAP_REGISTRY), names(registry.DEVICE_MAP_REGISTRY));
	const bundledDevices = await loadTypeScriptModule(
		'src/lib/components/rb/practice/practice-devices.ts'
	);
	assert.deepEqual(
		Object.keys(bundledDevices.PRACTICE_DEVICE_MAPS),
		Object.keys(devices.PRACTICE_DEVICE_MAPS)
	);
});
