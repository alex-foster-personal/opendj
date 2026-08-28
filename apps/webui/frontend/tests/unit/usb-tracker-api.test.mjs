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

test('simulateUsbVolume POSTs the exact body and maps non-2xx to simulate USB HTTP', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse({ volumes: [], scanned_at: 0, watching: false });
	};

	await usbTrackerApi.simulateUsbVolume({ name: 'FAKE USB', kind: 'music' });

	assert.equal(seen.url, `${API_BASE}/api/v1/usb/volumes`);
	assert.equal(seen.method, 'POST');
	assert.equal(seen.headers.get('content-type'), 'application/json');
	assert.deepEqual(body, {
		name: 'FAKE USB',
		kind: 'music',
		mount_path: '/Volumes/FAKE-USB',
		role: 'usb_stick',
		protocol: 'USB',
		present: true
	});

	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: 'nope' }), {
			status: 422,
			statusText: 'Unprocessable Entity',
			headers: { 'content-type': 'application/json' }
		});

	await assert.rejects(
		() => usbTrackerApi.simulateUsbVolume(),
		/simulate USB HTTP 422/
	);
});
