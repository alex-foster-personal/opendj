// Live-refresh wiring for the MIDI panel's installed device maps
// (src/lib/components/rb/midi/midi-ui-state.svelte.ts).
//
// events-bus.ts's own contract is explicit: "A consumer that handles a kind
// MUST also handle resync, otherwise it goes stale exactly when the bus is
// least reliable." midi-ui-state.svelte.ts used to subscribe only to the
// targeted 'midi_maps' kind, so a PUT/DELETE published while the socket was
// down for a gap, a slow-consumer close, or a reconnect never reached the
// registry - see PR #511 review thread "Resync installed maps after
// event-bus gaps".
//
// One shared vite SSR module graph (same idiom as midi-installed-maps.test.mjs)
// so uiState's import of events-bus.ts and installed-maps.ts resolves to the
// SAME singleton this file also drives directly - a fake WebMIDI navigator
// (midi-hotplug.test.mjs idiom) lets requestMidiAccess() reach the arm site,
// a real throwaway daemon (midi-installed-maps.test.mjs idiom) lets
// loadInstalledDeviceMaps() actually succeed, and a fake WebSocket (the same
// shape events-bus.test.mjs uses) lets this file fire a resync deterministically.
//
// Regression line:
//   if a resync fires with no matching subscribeKind event and the installed
//     registry does not reload then broken (the exact bug this file guards)

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
const WS_URL = 'ws://events.example.test/api/v1/events';

let vite;
let uiState;
let webmidi;
let installed;
let eventsBus;
let backendProc;
let apiOrigin;
let realFetch;

/** Minimal fake WebSocket: the bus only ever assigns the four handlers and
 * calls close(). Mirrors events-bus.test.mjs's FakeSocket. */
class FakeSocket {
	constructor(url) {
		this.url = url;
		this.onopen = null;
		this.onmessage = null;
		this.onclose = null;
		this.onerror = null;
	}
	open() {
		this.onopen?.({});
	}
	deliverRaw(data) {
		this.onmessage?.({ data });
	}
	hello({ contractRev = 'rev-1', engineVersion = '1.0.0', seqStart = 0, topics = [] } = {}) {
		this.deliverRaw(
			JSON.stringify({
				topic: 'hello',
				seq: seqStart,
				ts: '2026-08-19T10:00:00.000Z',
				payload: { contract_rev: contractRev, engine_version: engineVersion, seq_start: seqStart, topics }
			})
		);
	}
	close() {}
}

function makeScheduler() {
	return { setTimeout: () => 0, clearTimeout: () => {} };
}

function _startBackend() {
	return new Promise((resolvePort, reject) => {
		const proc = spawn(
			'uv',
			['run', '--no-sync', 'python', '-m', 'scripts.testing.midi_maps_test_server', '--port', '0'],
			{ cwd: REPO_ROOT, stdio: ['ignore', 'pipe', 'pipe'] }
		);
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

// A resync-driven reload runs async off the bus callback through REAL uvicorn
// round trips (GET /maps, then one GET per document), so its length belongs
// to the runner, not to this test. A fixed 50 ms sleep here expired before the
// reload landed under `--test-concurrency=4` on a loaded CI box (main
// 5d703636 and #3701 f1c3c138, Mon 21 Sep 2026: the sibling test on the same
// runner took 13 s) and read as the arm block being broken. Wait for the
// PRESENCE of the reloaded map with a bounded deadline instead; null after
// the deadline is the same failure the assertion always reported.
const RELOAD_DEADLINE_MS = 15_000;
async function _waitForMap(portName) {
	const started = Date.now();
	for (;;) {
		if (webmidi.resolveMapForPort(portName) !== null) {
			// One macrotask more: registerDeviceMap() runs inside the reload,
			// and the coalesced wrapper clears installedMapsError only when
			// that promise settles, one microtask later.
			await new Promise((r) => setTimeout(r, 0));
			return webmidi.resolveMapForPort(portName);
		}
		if (Date.now() - started > RELOAD_DEADLINE_MS) return null;
		await new Promise((r) => setTimeout(r, 10));
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
	uiState = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-ui-state.svelte.ts');
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	installed = await vite.ssrLoadModule('/src/lib/rb/midi/installed-maps.ts');
	eventsBus = await vite.ssrLoadModule('/src/lib/api/events-bus.ts');
	_installRealFetchProxy();
});

after(async () => {
	await vite.close();
	globalThis.fetch = realFetch;
	backendProc?.kill();
});

const DOC_A = {
	schemaVersion: 1,
	id: 'aaa-device',
	vendor: 'AaaCo',
	model: 'AaaDevice 1',
	nameMatch: 'AaaDevice',
	bindings: [
		{
			source: { ch: 1, kind: 'note', id: 11 },
			action: { type: 'deck_play_toggle', deck: 1 },
			provenance: { tier: 'learned', cite: 'learn wizard, port AaaDevice', verified: true }
		}
	]
};

const DOC_B = {
	...DOC_A,
	id: 'bbb-device',
	vendor: 'BbbCo',
	nameMatch: 'BbbDevice'
};

beforeEach(async () => {
	webmidi._resetMidiForTests();
	installed._resetInstalledMapsForTests();
	eventsBus._resetForTests();
	uiState._resetInstalledMapsLiveRefreshArmedForTests();
	uiState.midiUi.lastError = null;
	uiState.midiUi.installedMapsError = null;
	await _uninstall('aaa-device');
	await _uninstall('bbb-device');
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		writable: true,
		value: { requestMIDIAccess: async () => ({ inputs: new Map(), outputs: new Map(), onstatechange: null }) }
	});
});

test('a resync with no matching kind event still reloads the installed registry', async () => {
	await _install(DOC_A);

	let socket = null;
	eventsBus.connect(WS_URL, {
		socketFactory: (url) => {
			socket = new FakeSocket(url);
			return socket;
		},
		scheduler: makeScheduler()
	});
	socket.open();
	socket.hello();

	await uiState.requestMidiAccess();
	assert.equal(uiState.midiUi.lastError, null, 'fake WebMIDI access must succeed');
	assert.equal(uiState.midiUi.installedMapsError, null);
	assert.notEqual(webmidi.resolveMapForPort('AaaDevice 1'), null);

	// A second map is installed directly against the daemon - simulating a
	// PUT that happened while the socket was down, so no 'midi_maps' kind
	// event was ever delivered to this page. Not yet visible to the runtime.
	await _install(DOC_B);
	assert.equal(webmidi.resolveMapForPort('BbbDevice 1'), null);

	// A malformed frame is read by events-bus.ts as an unconditional gap:
	// "we cannot know what this frame carried" - the same resync path a real
	// seq skip, a 1013 slow-consumer close, or a reconnect would take.
	socket.deliverRaw('not json');

	// loadInstalledDeviceMaps() runs async off the resync callback; wait for
	// its fetch chain to land (bounded, see _waitForMap).
	assert.notEqual(
		await _waitForMap('BbbDevice 1'),
		null,
		'resync alone must reload the installed registry, per events-bus.ts: ' +
			'"a consumer that handles a kind MUST also handle resync"'
	);
	assert.equal(uiState.midiUi.installedMapsError, null);

	eventsBus.disconnect();
	await _uninstall('aaa-device');
	await _uninstall('bbb-device');
});

test('a failed first load of installed maps still arms the live-refresh listeners', async () => {
	// requestMidiAccess() used to arm subscribeKind/subscribeResync only
	// INSIDE the try, after loadInstalledDeviceMaps() succeeded - so a daemon
	// hiccup on the very first load left the listeners permanently unarmed for
	// the rest of the page's life: a later resync (reconnect/gap) would never
	// reload the registry even after the daemon came back. See PR #511 review
	// thread "Arm live refresh before the first map load".
	const unreachableOrigin = 'http://127.0.0.1:1'; // refuses immediately (port 1)
	globalThis.fetch = (input, init) => {
		const url = typeof input === 'string' ? input : input.url;
		const absolute = /^https?:\/\//.test(url) ? url : `${unreachableOrigin}${url}`;
		return realFetch(absolute, init);
	};

	let socket = null;
	eventsBus.connect(WS_URL, {
		socketFactory: (url) => {
			socket = new FakeSocket(url);
			return socket;
		},
		scheduler: makeScheduler()
	});
	socket.open();
	socket.hello();

	await uiState.requestMidiAccess();
	assert.notEqual(
		uiState.midiUi.installedMapsError,
		null,
		'the first load against an unreachable daemon must fail loudly'
	);

	// The daemon is reachable again: point fetch back at apiOrigin (NOT via
	// _installRealFetchProxy(), which would recapture "real" fetch from the
	// unreachable-origin proxy currently installed above and recurse forever).
	globalThis.fetch = (input, init) => {
		const url = typeof input === 'string' ? input : input.url;
		const absolute = /^https?:\/\//.test(url) ? url : `${apiOrigin}${url}`;
		return realFetch(absolute, init);
	};
	await _install(DOC_A);
	assert.equal(webmidi.resolveMapForPort('AaaDevice 1'), null);

	socket.deliverRaw('not json'); // gap/resync, same as the test above

	assert.notEqual(
		await _waitForMap('AaaDevice 1'),
		null,
		'resync must still reload the registry even though the FIRST load failed - ' +
			'the arm block must run unconditionally, not only after a successful load'
	);
	assert.equal(uiState.midiUi.installedMapsError, null);

	eventsBus.disconnect();
	await _uninstall('aaa-device');
});
