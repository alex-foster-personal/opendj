// Installed controller maps: fetch from the daemon, register in the
// 'installed' tier, uninstall what the daemon stopped serving.
// Same vite+svelte bootstrap as the other rune-module suites.
//
// Exercises the REAL midi_maps FastAPI route (AGENTS.md "No mocks and locked
// real fixtures"): a throwaway daemon (scripts/testing/midi_maps_test_server.py)
// is spawned on a real loopback port for the whole file, seeded via real PUT
// requests, and torn down via real DELETE requests. `globalThis.fetch` is
// replaced only to route relative '/api/...' paths at that real origin - the
// same job the Vite dev proxy does in production - never to fabricate a
// response body or status.
//
// Regression lines (single-line format per CLAUDE.md):
//   if a loaded map does not win over a builtin on the same nameMatch then broken
//   if a second load throws 'already registered' then broken (no reload path)
//   if a map the daemon dropped still resolves after a reload then broken
//   if provenance reaches the runtime DeviceMap then broken (dispatch data only)
//   if a non-ok response resolves instead of throwing then broken

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { after, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPO_ROOT = resolve(FRONTEND_ROOT, '../../..');
const START_TIMEOUT_MS = 20_000;

let vite;
let webmidi;
let installed;
let backendProc;
let apiOrigin;
let realFetch;

/** Boot the throwaway real backend and wait for its LISTENING line. Never
 * fabricates a port: reads back whatever uvicorn actually bound. */
function _startBackend() {
	return new Promise((resolvePort, reject) => {
		const proc = spawn('uv', ['run', '--no-sync', 'python', '-m', 'scripts.testing.midi_maps_test_server', '--port', '0'], {
			cwd: REPO_ROOT,
			stdio: ['ignore', 'pipe', 'pipe']
		});
		const timer = setTimeout(() => {
			proc.kill();
			reject(new Error(`midi_maps_test_server did not report LISTENING within ${START_TIMEOUT_MS}ms`));
		}, START_TIMEOUT_MS);
		let stderr = '';
		proc.stderr.on('data', (chunk) => {
			stderr += String(chunk);
		});
		proc.on('exit', (code) => {
			if (code !== null && code !== 0) {
				clearTimeout(timer);
				reject(new Error(`midi_maps_test_server exited ${code}:\n${stderr}`));
			}
		});
		const lines = createInterface({ input: proc.stdout });
		lines.on('line', (line) => {
			const match = /^LISTENING (\d+)$/.exec(line);
			if (match) {
				clearTimeout(timer);
				lines.close();
				backendProc = proc;
				resolvePort(Number(match[1]));
			}
		});
	});
}

/** Route any '/api/...' path at the real backend origin, exactly what the
 * Vite dev proxy does in production - the real fetch still runs. */
function _installRealFetchProxy() {
	realFetch = globalThis.fetch;
	globalThis.fetch = (input, init) => {
		const url = typeof input === 'string' ? input : input.url;
		const absolute = /^https?:\/\//.test(url) ? url : `${apiOrigin}${url}`;
		return realFetch(absolute, init);
	};
}

async function _install(doc) {
	const r = await realFetch(`${apiOrigin}/api/v1/midi/maps/${doc.id}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(doc)
	});
	if (!r.ok) throw new Error(`test setup: PUT ${doc.id} failed ${r.status}: ${await r.text()}`);
}

async function _uninstall(id) {
	const r = await realFetch(`${apiOrigin}/api/v1/midi/maps/${id}`, { method: 'DELETE' });
	if (!r.ok && r.status !== 404) {
		throw new Error(`test teardown: DELETE ${id} failed ${r.status}: ${await r.text()}`);
	}
}

before(async () => {
	const port = await _startBackend();
	apiOrigin = `http://127.0.0.1:${port}`;
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		server: { middlewareMode: true },
		appType: 'custom',
		plugins: [svelte()],
		logLevel: 'silent',
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	installed = await vite.ssrLoadModule('/src/lib/rb/midi/installed-maps.ts');
	_installRealFetchProxy();
});

after(async () => {
	await vite.close();
	globalThis.fetch = realFetch;
	backendProc?.kill();
});

const DOC = {
	schemaVersion: 1,
	id: 'test-device',
	vendor: 'InstalledCo',
	model: 'TestDevice 1',
	nameMatch: 'TestDevice',
	bindings: [
		{
			source: { ch: 1, kind: 'note', id: 11 },
			action: { type: 'deck_play_toggle', deck: 1 },
			provenance: { tier: 'learned', cite: 'learn wizard, port TestDevice', verified: true }
		}
	]
};

beforeEach(async () => {
	webmidi._resetMidiForTests();
	installed._resetInstalledMapsForTests();
	await _uninstall('test-device');
});

test('deviceMapFromDoc strips provenance, which is UI metadata not dispatch data', () => {
	const map = installed.deviceMapFromDoc(DOC);
	assert.equal(map.vendor, 'InstalledCo');
	assert.equal(map.bindings.length, 1);
	assert.equal('provenance' in map.bindings[0], false);
	assert.deepEqual(map.bindings[0].source, { ch: 1, kind: 'note', id: 11 });
});

test('a loaded map wins over a builtin on the same port name', async () => {
	webmidi.registerDeviceMap(
		{
			vendor: 'BuiltinCo',
			nameMatch: 'TestDevice',
			bindings: [
				{ source: { ch: 1, kind: 'note', id: 99 }, action: { type: 'deck_cue', deck: 1 } }
			]
		},
		'builtin'
	);
	await _install(DOC);
	const loaded = await installed.loadInstalledDeviceMaps();
	assert.equal(loaded.length, 1);
	assert.equal(webmidi.resolveMapForPort('TestDevice 1')?.vendor, 'InstalledCo');
	// The builtin is shadowed, not destroyed, so an uninstall reverts to it.
	assert.equal(installed.shadowedBuiltin('TestDevice')?.vendor, 'BuiltinCo');
});

test('loading twice replaces rather than throwing, so a wizard save needs no reload', async () => {
	await _install(DOC);
	await installed.loadInstalledDeviceMaps();
	const grown = {
		...DOC,
		bindings: [
			...DOC.bindings,
			{
				source: { ch: 1, kind: 'note', id: 12 },
				action: { type: 'deck_cue', deck: 1 },
				provenance: { tier: 'learned', cite: 'learn wizard, port TestDevice', verified: true }
			}
		]
	};
	await _install(grown);
	await installed.loadInstalledDeviceMaps();
	assert.equal(webmidi.resolveMapForPort('TestDevice 1')?.bindings.length, 2);
	assert.equal(webmidi.listDeviceMaps().filter((e) => e.tier === 'installed').length, 1);
});

test('a map the daemon stopped serving is unregistered on the next load', async () => {
	await _install(DOC);
	await installed.loadInstalledDeviceMaps();
	assert.notEqual(webmidi.resolveMapForPort('TestDevice 1'), null);
	await _uninstall('test-device');
	await installed.loadInstalledDeviceMaps();
	assert.equal(webmidi.resolveMapForPort('TestDevice 1'), null);
	assert.equal(installed.installedProvenance.size, 0);
});

test('provenance is kept alongside the map, keyed by id, for the UI to read', async () => {
	await _install(DOC);
	await installed.loadInstalledDeviceMaps();
	assert.equal(installed.installedProvenance.get('test-device')[0].tier, 'learned');
});

test('a document fetch failure mid-reload does not apply a partial generation', async () => {
	// Sorted filename order matters: 'aaa-device' is listed and fetched
	// BEFORE 'zzz-device', so a naive per-map commit loop would already have
	// switched aaa-device to its generation-2 nameMatch by the time
	// zzz-device's fetch throws. All-or-nothing means it must not have.
	// nameMatch is matched as a regex substring (resolveMapForPort), so the
	// two generations use tokens with no substring overlap in either
	// direction - otherwise a stale registration could pass by accident.
	const first = { ...DOC, id: 'aaa-device', nameMatch: 'AaaOriginal' };
	const second = { ...DOC, id: 'zzz-device', nameMatch: 'ZzzDevice', vendor: 'ZzzCo' };
	await _install(first);
	await _install(second);
	await installed.loadInstalledDeviceMaps();
	assert.notEqual(webmidi.resolveMapForPort('AaaOriginal'), null);

	// Generation 2 for aaa-device only.
	await _install({ ...first, nameMatch: 'AaaUpdated' });

	const proxied = globalThis.fetch;
	globalThis.fetch = async (input, init) => {
		const url = typeof input === 'string' ? input : input.url;
		if (url.endsWith('/api/v1/midi/maps/zzz-device')) {
			await _uninstall('zzz-device');
		}
		return proxied(input, init);
	};
	try {
		await assert.rejects(() => installed.loadInstalledDeviceMaps());
	} finally {
		globalThis.fetch = proxied;
	}

	assert.notEqual(webmidi.resolveMapForPort('AaaOriginal'), null);
	assert.equal(webmidi.resolveMapForPort('AaaUpdated'), null);

	await _uninstall('aaa-device');
});

test('a nameMatch the browser RegExp rejects does not destroy the previous generation', async () => {
	// Atomic groups compile under Python's re (so they pass the daemon's
	// necessarily-partial deny list, routes/midi_maps.py) but throw in the
	// browser's RegExp engine - PR #511 review thread "Validate the full
	// JavaScript regex grammar". validateDeviceMap() must catch this before
	// loadInstalledDeviceMaps() unregisters the previous generation.
	const first = { ...DOC, id: 'aaa-device', nameMatch: 'AaaOriginal' };
	await _install(first);
	await installed.loadInstalledDeviceMaps();
	assert.notEqual(webmidi.resolveMapForPort('AaaOriginal'), null);

	await _install({ ...DOC, id: 'bad-device', nameMatch: '(?>Bad)' });

	await assert.rejects(() => installed.loadInstalledDeviceMaps(), /Invalid regular expression/);
	assert.notEqual(webmidi.resolveMapForPort('AaaOriginal'), null);

	await _uninstall('bad-device');
	await _uninstall('aaa-device');
});

test('a duplicate nameMatch batch is rejected before it corrupts the registry', async () => {
	// A concurrent PUT can leave two persisted documents with one nameMatch
	// because the route's check and write are separate operations. Exercise
	// the loader's batch validator directly with the real document fixture:
	// the test must not manufacture a transport response for a server state
	// that it did not actually receive.
	const first = { ...DOC, id: 'aaa-device', nameMatch: 'AaaShared' };
	const second = { ...DOC, id: 'bbb-device', nameMatch: 'AaaShared', vendor: 'BbbCo' };
	await _install(first);
	await installed.loadInstalledDeviceMaps();
	assert.equal(webmidi.resolveMapForPort('AaaShared')?.vendor, 'InstalledCo');

	assert.throws(
		() => installed.validateInstalledMapBatch([first, second]),
		/both claim nameMatch 'AaaShared'/
	);

	// Previous generation must still be intact: the clash must be caught
	// before the unregister loop touches the registry at all.
	assert.equal(webmidi.resolveMapForPort('AaaShared')?.vendor, 'InstalledCo');
	assert.equal(webmidi.listDeviceMaps().filter((e) => e.tier === 'installed').length, 1);

	await _uninstall('aaa-device');
});

test('a backend error propagates instead of resolving to an empty registry', async () => {
	// A real 404 from a real race: the map is deleted from the real daemon
	// between the list call and the per-map GET loadInstalledDeviceMaps()
	// issues next, so the GET genuinely 404s (MIDI_MAP_NOT_FOUND) rather
	// than a fabricated error body.
	await _install(DOC);
	const proxied = globalThis.fetch;
	let racedOnce = false;
	globalThis.fetch = async (input, init) => {
		const response = await proxied(input, init);
		const url = typeof input === 'string' ? input : input.url;
		if (!racedOnce && url.endsWith('/api/v1/midi/maps')) {
			racedOnce = true;
			await _uninstall('test-device');
		}
		return response;
	};
	await assert.rejects(() => installed.loadInstalledDeviceMaps(), /no installed map with id 'test-device'/);
});
