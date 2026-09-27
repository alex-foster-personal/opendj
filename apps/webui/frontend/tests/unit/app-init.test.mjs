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
 * - if startAppInstruments stops opening the boot request window then the
 *   deferred boot calls have nothing holding them back and PERF-R6's 2.1x
 *   startup deck-load regression comes straight back.
 * - if startAppInstruments stops installing __mdtScheduleReload then REFRESH-01
 *   has no agent-facing trigger and a reload can still land with no warning.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, test } from 'node:test';

import { immediateBootScheduler, manualBootScheduler } from './fake-boot-scheduler.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://engine.example.test';
const PERF_TIER_BODY = JSON.stringify({
	tier: 'STANDARD',
	source: 'auto',
	auto_tier: 'STANDARD',
	override: 'auto'
});

let appInit;
let originalFetch;
let posted;
let fetchedUrls;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function installBrowserGlobals() {
	const storage = new Map();
	const localStorage = {
		getItem: (key) => storage.get(key) ?? null,
		setItem: (key, value) => storage.set(key, String(value))
	};
	defineGlobal('localStorage', localStorage);
	defineGlobal('window', {
		location: { origin: 'https://app.example.test', pathname: '/' },
		localStorage
	});
	defineGlobal('document', {
		visibilityState: 'visible',
		documentElement: { dataset: {}, style: {} },
		addEventListener: () => {},
		removeEventListener: () => {},
		createElement: () => ({ id: '', style: {}, textContent: '' }),
		body: { appendChild() {} },
		// The reload announcer (REFRESH-01) owns one overlay element and looks
		// it up by id on render and teardown. Null is the honest answer here:
		// this fake has never been asked to create one.
		getElementById: () => null
	});
	defineGlobal('crypto', { randomUUID: () => 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' });
	posted = [];
	fetchedUrls = [];
	globalThis.fetch = async (input, init) => {
		// The typed openapi-fetch client (telemetry consent) passes a Request;
		// the hand-rolled fetches pass a string. Same URL either way.
		const url = typeof input === 'string' ? input : input.url;
		fetchedUrls.push(url);
		// The heartbeat POSTs a JSON body; the machine-pressure poll GETs with
		// none, so only parse when one was actually sent.
		if (init?.body !== undefined) posted.push({ url, body: JSON.parse(init.body) });
		// The deferred perf-tier fetch needs a real tier: an empty body resolves
		// the tier to undefined and prefetchTrackCap() throws inside init.
		const body = String(url).endsWith('/api/v1/perf-tier') ? PERF_TIER_BODY : '{}';
		return new Response(body, { status: 200, headers: { 'content-type': 'application/json' } });
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

	const stop = appInit.startAppInstruments(immediateBootScheduler());

	assert.equal(typeof window.__mdtPerfLog, 'function');
	assert.equal(typeof window.__mdtLastLoads, 'function');
	assert.ok(Array.isArray(window.__mdtPerfLog()));
	assert.ok(Array.isArray(window.__mdtLastLoads()));
	stop();
});

// Teardown is registered before any assertion: a failing assertion that skips
// stop() leaves the heartbeat and pressure timers alive, and node --test then
// never exits (the whole unit suite hung on this, Mon 14 Sep 2026).
test('init also starts the usage heartbeat', async (t) => {
	const stop = appInit.startAppInstruments(immediateBootScheduler());
	t.after(stop);
	await new Promise((resolve) => setImmediate(resolve));

	assert.ok(
		posted.some((entry) => entry.url === `${API_BASE}/api/v1/telemetry/heartbeat`),
		'the usage heartbeat must post after init'
	);
});

test('init opens the boot request window and hands its teardown back', () => {
	// PERF-R6: the deferred boot calls are held by THIS scheduler, so the
	// root layout starting the instruments is what opens the window, and the
	// teardown it returns is what closes it.
	const started = [];
	const stopped = [];
	const scheduler = {
		defer: (_label, task) => task(),
		deckLoadStarted: () => () => {},
		start: () => {
			started.push(true);
			return () => stopped.push(true);
		}
	};

	const stop = appInit.startAppInstruments(scheduler);
	assert.equal(started.length, 1, 'the boot window must be opened exactly once');
	assert.equal(stopped.length, 0);

	stop();
	assert.equal(stopped.length, 1, 'and closed when the page goes away');
});

test('the heartbeat and the pressure poll go through the same window, so neither joins the burst', async (t) => {
	const manual = manualBootScheduler();
	const stop = appInit.startAppInstruments(manual.scheduler);
	t.after(stop);
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(posted.length, 0, 'nothing may post while the boot window is open');
	assert.equal(fetchedUrls.length, 0, 'nothing may fetch while the boot window is open');
	assert.equal(
		manual.pending(),
		5,
		'the perf-tier fetch, telemetry consent, heartbeat, pressure poll, and client samples are all queued, never dropped'
	);

	manual.release();
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(posted.length, 2);
	assert.ok(
		fetchedUrls.some((url) => url.endsWith('/api/v1/perf-tier')),
		'the perf-tier fetch also waited for the window'
	);
	assert.ok(
		posted.some((entry) => entry.url.includes('/performance/telemetry/client-samples')),
		'client samples also waited for the boot window'
	);
	assert.ok(
		fetchedUrls.some((url) => url.includes('/telemetry/pressure')),
		'the pressure poll also waited for the window, not just the heartbeat'
	);
});

test('init installs the reload announcer, and its teardown removes it', () => {
	// REFRESH-01 (#891). Same failure shape this file was written for: an
	// installer that works fine and that nothing calls.
	assert.equal(window.__mdtScheduleReload, undefined);

	const stop = appInit.startAppInstruments(immediateBootScheduler());

	assert.equal(typeof window.__mdtScheduleReload, 'function');
	stop();
	assert.equal(window.__mdtScheduleReload, undefined);
});

test('startAppInstruments arms the background demand shed', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/app-init.ts', import.meta.url)),
		'utf8'
	);
	assert.match(source, /startBackgroundDemandShed/);
});

test('startAppInstruments registers the live-transport probe for client errors', () => {
	// if startAppInstruments stops registering anyDeckPlaying as the probe then
	// every client error carries any_deck_live: null and the engine's
	// "never send to Sentry while a deck is live" rule falls back to the
	// mirror read alone, which is stale for up to a second.
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/app-init.ts', import.meta.url)),
		'utf8'
	);
	assert.match(source, /setLiveTransportProbe\(anyDeckPlaying\)/);
	assert.match(source, /setLiveTransportProbe\(null\)/, 'teardown must clear the probe');
});

test('startAppInstruments wires silence dropout recovery', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/app-init.ts', import.meta.url)),
		'utf8'
	);
	assert.match(source, /setSilenceDropoutHandler/);
});

test('startAppInstruments wires silence source PCM reader', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/app-init.ts', import.meta.url)),
		'utf8'
	);
	assert.match(source, /setSilenceSourceReader\(readSilenceSourceDeckSnaps\)/);
	assert.match(
		source,
		/setSilenceSourceReader\(null\)/,
		'teardown must clear the source reader'
	);
});

test('the root layout actually calls startAppInstruments', () => {
	// The gap the rest of this file cannot see. Every test above drives
	// startAppInstruments directly, so all of them keep passing if the layout
	// stops calling it - which is precisely the failure this file was written
	// for, one level up: an installer that works fine and that nothing runs.
	// REFRESH-01 (#891) rides on this call, so a silent unwiring would ship a
	// countdown that never announces anything.
	const layout = readFileSync(
		fileURLToPath(new URL('../../src/routes/+layout.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(layout, /import \{ startAppInstruments \}/);
	assert.match(layout, /startAppInstruments\(\)/);
});
