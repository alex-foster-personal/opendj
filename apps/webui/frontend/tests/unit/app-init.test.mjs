/**
 * The once-per-page-load instrument bundle the root layout mounts.
 *
 * Regression guard for a hook that was exported and then never called:
 * installPerfEventLogGlobal() had no production caller, so
 * window.__mdtPerfLog did not exist at runtime and
 * tests/e2e/performance-controls.spec.ts failed with "the latency
 * instrument is missing". A unit test on perf-event-log itself could not
 * catch that, because the function worked fine -- nothing ran it.
 *
 * - if startAppInstruments stops installing __mdtPerfLog then the e2e
 *   latency floor cannot read the engine's own timings and is broken.
 * - if startAppInstruments stops sending a heartbeat then the engine can
 *   no longer tell whether the app is open and is broken.
 */
import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://engine.example.test';

let appInit;
let originalFetch;
let posted;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function installBrowserGlobals() {
	defineGlobal('window', { location: { origin: 'https://app.example.test', pathname: '/' } });
	defineGlobal('document', {
		visibilityState: 'visible',
		addEventListener: () => {},
		removeEventListener: () => {}
	});
	defineGlobal('crypto', { randomUUID: () => 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' });
	posted = [];
	globalThis.fetch = async (url, init) => {
		posted.push({ url, body: JSON.parse(init.body) });
		return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
	};
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserGlobals();
	appInit = await loadTypeScriptModule('src/lib/rb/app-init.ts', { viteApiBase: API_BASE });
});

afterEach(() => {
	installBrowserGlobals();
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('the DevTools perf log globals exist after init', () => {
	assert.equal(window.__mdtPerfLog, undefined);
	assert.equal(window.__mdtLastLoads, undefined);

	const stop = appInit.startAppInstruments();

	assert.equal(typeof window.__mdtPerfLog, 'function');
	assert.equal(typeof window.__mdtLastLoads, 'function');
	assert.ok(Array.isArray(window.__mdtPerfLog()));
	assert.ok(Array.isArray(window.__mdtLastLoads()));
	stop();
});

test('init also starts the usage heartbeat', async () => {
	const stop = appInit.startAppInstruments();
	await new Promise((resolve) => setImmediate(resolve));
	stop();

	assert.equal(posted.length, 1);
	assert.equal(posted[0].url, `${API_BASE}/api/v1/telemetry/heartbeat`);
});
