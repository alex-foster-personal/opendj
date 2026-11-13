import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, test } from 'node:test';

import { immediateBootScheduler, manualBootScheduler } from './fake-boot-scheduler.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://engine.example.test';

let samples;
let posted;
let visibilityHandlers;
let intervals;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function fakeDocument(initial) {
	return {
		visibilityState: initial,
		addEventListener(type, handler) {
			if (type === 'visibilitychange') visibilityHandlers.push(handler);
		},
		removeEventListener(type, handler) {
			if (type !== 'visibilitychange') return;
			visibilityHandlers = visibilityHandlers.filter((entry) => entry !== handler);
		}
	};
}

function deckStub(overrides = {}) {
	return {
		stable_id: 'stable-1',
		duration_ms: 120_000,
		playing: false,
		audible: false,
		transport_pending: false,
		stems: { status: 'unavailable' },
		last_load_latency_ms: null,
		sync_error: null,
		processor_error: null,
		...overrides
	};
}

before(async () => {
	samples = await loadTypeScriptModule('src/lib/rb/client-performance-samples.ts', {
		viteApiBase: API_BASE
	});
});

beforeEach(() => {
	posted = [];
	visibilityHandlers = [];
	intervals = [];
	defineGlobal('window', {
		location: { pathname: '/performance' },
		addEventListener() {},
		removeEventListener() {}
	});
	defineGlobal('document', fakeDocument('visible'));
	defineGlobal('performance', { now: () => 12_000 });
	defineGlobal('crypto', { randomUUID: () => 'sample-uuid' });
	defineGlobal('fetch', async (url, init) => {
		posted.push({ url, body: JSON.parse(init.body) });
		return new Response('{}', { status: 202 });
	});
	defineGlobal('setInterval', (fn, delayMs) => {
		const handle = { fn, delayMs };
		intervals.push(handle);
		return handle;
	});
	defineGlobal('clearInterval', () => {});
});

afterEach(() => {
	visibilityHandlers = [];
});

test('loading stems map to unavailable on the wire', () => {
	assert.equal(samples.mapStemStatus('loading'), 'unavailable');
});

test('error stems stay error on the wire', () => {
	assert.equal(samples.mapStemStatus('error'), 'error');
});

test('sync_error is copied into the deck sample', () => {
	const row = samples.deckPerformanceSampleFromState(2, deckStub({ sync_error: 'phase drift' }));
	assert.equal(row.sync_error, 'phase drift');
});

test('first POST is deferred until the boot window releases', async () => {
	const manual = manualBootScheduler();
	const stop = samples.startClientPerformanceSampling(manual.scheduler);
	await Promise.resolve();
	assert.equal(manual.pending(), 1);
	assert.equal(posted.length, 0);
	manual.release();
	await Promise.resolve();
	assert.equal(posted.length, 1);
	assert.equal(posted[0].url, `${API_BASE}/api/v1/performance/telemetry/client-samples`);
	stop();
});

test('hidden tab does not POST', async () => {
	globalThis.document = fakeDocument('hidden');
	const stop = samples.startClientPerformanceSampling(immediateBootScheduler());
	await Promise.resolve();
	assert.equal(posted.length, 0);
	stop();
});

// PERFPROBE-01: the frame probe rides the sample only when asked for.
test('without ?perfprobe=1 the sample carries frame_probe null and no probe starts', async () => {
	let started = 0;
	const stop = samples.startClientPerformanceSampling(immediateBootScheduler(), () => {
		started += 1;
		return { take: () => ({}), stop() {} };
	});
	await Promise.resolve();
	assert.equal(started, 0);
	assert.equal(posted[0].body.frame_probe, null);
	// PERF-LEAK-11789: no live handles probe, no totals.
	assert.equal(posted[0].body.live_handles, null);
	stop();
});

// PERF-LEAK-11789: the armed probe's four totals ride the sample, without by_site.
test('with the live handles probe armed the sample carries its four totals', async () => {
	globalThis.window.__mdtLiveHandles = () => ({
		intervals: 3,
		long_timeouts: 1,
		window_listeners: 7,
		rafs: 2,
		by_site: { a: 1 }
	});
	const stop = samples.startClientPerformanceSampling(immediateBootScheduler());
	await Promise.resolve();
	assert.deepEqual(posted[0].body.live_handles, {
		intervals: 3,
		long_timeouts: 1,
		window_listeners: 7,
		rafs: 2
	});
	stop();
});

test('the payload builder defaults live_handles to null', () => {
	assert.equal(samples.buildClientPerformanceSamplePayload('s', 'id').live_handles, null);
});

test('the payload builder posts readout_rows null when no perf card is mounted', () => {
	assert.equal(samples.buildClientPerformanceSamplePayload('s', 'id').readout_rows, null);
});

test('with ?perfprobe=1 each sample carries the probe window and stop stops the probe', async () => {
	globalThis.window.location.search = '?perfprobe=1';
	let stopped = 0;
	const window1 = { shell: 'electron', fps: 59.7 };
	const stop = samples.startClientPerformanceSampling(immediateBootScheduler(), () => ({
		take: () => window1,
		stop() {
			stopped += 1;
		}
	}));
	await Promise.resolve();
	assert.deepEqual(posted[0].body.frame_probe, window1);
	stop();
	assert.equal(stopped, 1);
});
