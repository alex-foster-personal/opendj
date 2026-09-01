/**
 * App-usage heartbeat: the client half of "is the app open, and is anyone
 * looking at it". Asserts it fires on start and on visibilitychange, and that
 * it names the surface it is actually running on.
 *
 * Shell detection is asserted against the signal the REAL shell was observed
 * to expose (Wed 19 Aug 2026, installed Open DJ.app 0.1.0 attached to a
 * loopback probe engine): `globalThis.OPENDJ_ENGINE_ORIGIN` survives the
 * webview's navigation from tauri://localhost to the engine origin, while
 * `window.__TAURI__` is undefined there.
 *
 * The FIRST check-in is deferred out of the boot request burst (PERF-R6), so
 * these cases hand startUsageHeartbeat an immediate scheduler: their subject
 * is the payload and the listener wiring, not the boot window. The deferral
 * itself gets its own case at the bottom, and its ordering is proven in
 * boot-scheduler.test.mjs.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, afterEach, before, test } from 'node:test';

import { immediateBootScheduler, manualBootScheduler } from './fake-boot-scheduler.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://engine.example.test';

let heartbeat;
let originalFetch;
let posted;
let visibilityHandlers;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function installBrowserGlobals({ visibilityState = 'visible', shell = false } = {}) {
	visibilityHandlers = [];
	const win = {
		location: { origin: 'https://app.example.test', pathname: '/' }
	};
	if (shell) win.OPENDJ_ENGINE_ORIGIN = 'http://127.0.0.1:8685';
	defineGlobal('window', win);
	defineGlobal('document', {
		visibilityState,
		addEventListener: (type, handler) => {
			if (type === 'visibilitychange') visibilityHandlers.push(handler);
		},
		removeEventListener: (type, handler) => {
			if (type !== 'visibilitychange') return;
			visibilityHandlers = visibilityHandlers.filter((entry) => entry !== handler);
		}
	});
	defineGlobal('crypto', { randomUUID: () => 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' });
	posted = [];
	globalThis.fetch = async (url, init) => {
		posted.push({ url, init, body: JSON.parse(init.body) });
		return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
	};
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserGlobals();
	heartbeat = await loadTypeScriptModule('src/lib/rb/usage-heartbeat.ts', {
		viteApiBase: API_BASE
	});
});

afterEach(() => {
	installBrowserGlobals();
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('the check-in names the surface, the visibility and the version', async () => {
	const stop = heartbeat.startUsageHeartbeat(immediateBootScheduler());
	await new Promise((resolve) => setImmediate(resolve));
	stop();

	assert.equal(posted.length, 1);
	assert.equal(posted[0].url, `${API_BASE}/api/v1/telemetry/heartbeat`);
	assert.equal(posted[0].init.method, 'POST');
	assert.equal(posted[0].init.keepalive, true);
	assert.deepEqual(posted[0].body, {
		client_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
		surface: 'browser',
		page_visible: true,
		app_version: heartbeat.APP_VERSION
	});
});

test('visibilitychange sends a fresh heartbeat carrying the new state', async () => {
	const stop = heartbeat.startUsageHeartbeat(immediateBootScheduler());
	await new Promise((resolve) => setImmediate(resolve));

	document.visibilityState = 'hidden';
	assert.equal(visibilityHandlers.length, 1);
	visibilityHandlers[0]();
	await new Promise((resolve) => setImmediate(resolve));
	stop();

	assert.equal(posted.length, 2);
	assert.equal(posted[1].body.page_visible, false);
});

test('stopping removes the listener so no further heartbeats fire', async () => {
	const stop = heartbeat.startUsageHeartbeat(immediateBootScheduler());
	await new Promise((resolve) => setImmediate(resolve));
	stop();

	assert.equal(visibilityHandlers.length, 0);
	assert.equal(posted.length, 1);
});

test('the desktop shell is detected from the origin the shell injects', async () => {
	installBrowserGlobals({ shell: true });
	const stop = heartbeat.startUsageHeartbeat(immediateBootScheduler());
	await new Promise((resolve) => setImmediate(resolve));
	stop();

	assert.equal(posted[0].body.surface, 'desktop-shell');
});

test('a plain browser tab is never reported as the desktop shell', () => {
	assert.equal(heartbeat.detectSurface({}), 'browser');
	assert.equal(heartbeat.detectSurface({ OPENDJ_ENGINE_ORIGIN: '' }), 'browser');
	assert.equal(
		heartbeat.detectSurface({ OPENDJ_ENGINE_ORIGIN: 'http://127.0.0.1:8685' }),
		'desktop-shell'
	);
});

test('an engine that is down does not throw into the app', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('connection refused');
	};
	let stop;
	assert.doesNotThrow(() => {
		stop = heartbeat.startUsageHeartbeat(immediateBootScheduler());
	});
	await new Promise((resolve) => setImmediate(resolve));
	stop();
});

test('the first check-in waits for the boot window instead of joining the burst', async () => {
	// PERF-R6: the boot burst is what a deck load at startup competes with.
	// Three heartbeat intervals fit inside the engine's 45s liveness window,
	// so a first check-in that lands a few seconds later still reads as live.
	// [if the first heartbeat posts at mount then it is back in the burst]
	const manual = manualBootScheduler();
	const stop = heartbeat.startUsageHeartbeat(manual.scheduler);
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(posted.length, 0, 'no check-in may go out during the boot window');
	assert.equal(manual.pending(), 1, 'and it must be queued, never dropped');

	manual.release();
	await new Promise((resolve) => setImmediate(resolve));
	stop();

	assert.equal(posted.length, 1);
	assert.equal(posted[0].url, `${API_BASE}/api/v1/telemetry/heartbeat`);
});

test('APP_VERSION stays in step with the package it ships from', () => {
	const pkg = JSON.parse(
		readFileSync(new URL('../../package.json', import.meta.url), 'utf8')
	);
	assert.equal(heartbeat.APP_VERSION, pkg.version);
});
