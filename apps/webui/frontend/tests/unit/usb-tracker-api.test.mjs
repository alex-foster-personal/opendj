import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://usb-tracker-api.example.test';

let usbTrackerApi;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	usbTrackerApi = await loadTypeScriptModule('src/lib/rb/usb-tracker.svelte.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('refreshUsbVolumes hits GET /api/v1/usb/volumes and clears lastError', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({ volumes: [], scanned_at: 0, watching: false });
	};

	usbTrackerApi.usbTracker.lastError = 'stale';
	await usbTrackerApi.refreshUsbVolumes();

	assert.equal(seen.url, `${API_BASE}/api/v1/usb/volumes`);
	assert.equal(seen.method, 'GET');
	assert.equal(usbTrackerApi.usbTracker.lastError, null);
});

test('refreshUsbVolumes maps a non-2xx to lastError without throwing', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: 'boom' }), {
			status: 500,
			statusText: 'Internal Server Error',
			headers: { 'content-type': 'application/json' }
		});

	await usbTrackerApi.refreshUsbVolumes();

	assert.equal(usbTrackerApi.usbTracker.lastError, 'usb volumes HTTP 500');
});

test('refreshUsbVolumes stores an unreachable-daemon message in lastError', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};

	await usbTrackerApi.refreshUsbVolumes();

	assert.equal(usbTrackerApi.usbTracker.lastError, 'fetch failed');
});

// ----- failing endpoint: named state + backoff (Thu 1 Oct 2026) -------------
//
// The preview engine answered 503 diskutil_unavailable and the tracker asked
// again every 5 s for as long as the page was open, with nothing on screen
// saying why the USB list was empty.

function unavailableResponse(reason, extra = {}) {
	return new Response(
		JSON.stringify({
			detail: { code: 'usb_volume_discovery_unavailable', reason, ...extra }
		}),
		{
			status: 503,
			statusText: 'Service Unavailable',
			headers: { 'content-type': 'application/json' }
		}
	);
}

async function settle() {
	// Real macrotask turns (setImmediate is not mocked): lets the fetch
	// promise chain finish before the next timer tick is asked for.
	for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

test('usbPollDelayMs: 5 s while healthy, doubling per failure, capped at 60 s', () => {
	const { usbPollDelayMs } = usbTrackerApi;
	assert.equal(usbPollDelayMs(0), 5000);
	assert.equal(usbPollDelayMs(1), 10000);
	assert.equal(usbPollDelayMs(2), 20000);
	assert.equal(usbPollDelayMs(3), 40000);
	assert.equal(usbPollDelayMs(4), 60000);
	assert.equal(usbPollDelayMs(50), 60000);
});

test('a 503 refusal becomes a named unavailable state carrying the daemon reason', async () => {
	globalThis.fetch = async () => unavailableResponse('diskutil_unavailable');

	await usbTrackerApi.refreshUsbVolumes();

	assert.equal(usbTrackerApi.usbTracker.discovery, 'unavailable');
	assert.equal(usbTrackerApi.usbTracker.unavailableReason, 'diskutil_unavailable');
	assert.match(usbTrackerApi.usbDiscoveryNotice(usbTrackerApi.usbTracker), /diskutil/);
});

test('an unreachable daemon is its own named state, not a refusal', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};

	await usbTrackerApi.refreshUsbVolumes();

	assert.equal(usbTrackerApi.usbTracker.discovery, 'unreachable');
	assert.equal(usbTrackerApi.usbTracker.unavailableReason, null);
});

test('a successful poll clears the named state and the failure count', async () => {
	globalThis.fetch = async () => unavailableResponse('diskutil_unavailable');
	await usbTrackerApi.refreshUsbVolumes();
	globalThis.fetch = async () => jsonResponse({ volumes: [], scanned_at: 7, watching: true });

	await usbTrackerApi.refreshUsbVolumes();

	assert.equal(usbTrackerApi.usbTracker.discovery, 'ok');
	assert.equal(usbTrackerApi.usbTracker.unavailableReason, null);
	assert.equal(usbTrackerApi.usbTracker.consecutiveFailures, 0);
	assert.equal(usbTrackerApi.usbDiscoveryNotice(usbTrackerApi.usbTracker), null);
});

test('a failing endpoint is asked 3 times in the first minute, not 13', async (t) => {
	t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
	// The tracker is module state shared by every test in this file.
	usbTrackerApi.usbTracker.consecutiveFailures = 0;
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return unavailableResponse('diskutil_unavailable');
	};
	try {
		usbTrackerApi.startUsbWatch();
		await settle();
		// One-second steps for 60 s: polls land at 0 s, 10 s and 30 s; the
		// next is due at 70 s. The fixed 5 s interval made 13.
		for (let second = 0; second < 60; second += 1) {
			t.mock.timers.tick(1000);
			await settle();
		}
		assert.equal(calls, 3);
	} finally {
		usbTrackerApi.stopUsbWatch();
	}
});

test('a healthy endpoint is still polled every 5 s (backoff must not stick)', async (t) => {
	t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
	// The tracker is module state shared by every test in this file.
	usbTrackerApi.usbTracker.consecutiveFailures = 0;
	let calls = 0;
	let failing = true;
	globalThis.fetch = async () => {
		calls += 1;
		return failing
			? unavailableResponse('diskutil_unavailable')
			: jsonResponse({ volumes: [], scanned_at: calls, watching: true });
	};
	try {
		usbTrackerApi.startUsbWatch();
		await settle();
		failing = false;
		// The poll already scheduled 10 s out is the one that recovers.
		for (let second = 0; second < 10; second += 1) {
			t.mock.timers.tick(1000);
			await settle();
		}
		assert.equal(calls, 2);
		for (let second = 0; second < 20; second += 1) {
			t.mock.timers.tick(1000);
			await settle();
		}
		// 20 s of health at 5 s per poll.
		assert.equal(calls, 6);
	} finally {
		usbTrackerApi.stopUsbWatch();
	}
});

test('stopUsbWatch ends the chain: no request after it', async (t) => {
	t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
	// The tracker is module state shared by every test in this file.
	usbTrackerApi.usbTracker.consecutiveFailures = 0;
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse({ volumes: [], scanned_at: calls, watching: true });
	};
	usbTrackerApi.startUsbWatch();
	await settle();
	usbTrackerApi.stopUsbWatch();
	const before = calls;
	t.mock.timers.tick(120000);
	await settle();
	assert.equal(calls, before);
});
