/**
 * Durable client-error queue after conversion onto the generated OpenAPI client.
 * 2xx drains; non-2xx leaves the head item queued for the next report.
 */
import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://client-errors.example.test';
const QUEUE_KEY = 'music-dj-tools:client-errors:v1';

let reporting;
let originalFetch;
let store;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function makeLocalStorage() {
	const map = new Map();
	return {
		getItem: (key) => (map.has(key) ? map.get(key) : null),
		setItem: (key, value) => {
			map.set(key, String(value));
		},
		removeItem: (key) => {
			map.delete(key);
		},
		_map: map
	};
}

function installBrowserGlobals() {
	store = makeLocalStorage();
	defineGlobal('window', {
		location: { href: 'https://app.example.test/performance' },
		isSecureContext: true,
		localStorage: store,
		addEventListener: () => {}
	});
	defineGlobal('localStorage', store);
	defineGlobal('navigator', { userAgent: 'error-reporting-test-agent' });
	defineGlobal('crypto', {
		randomUUID: () => 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
	});
	defineGlobal('AudioWorkletNode', function AudioWorkletNode() {});
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserGlobals();
	reporting = await loadTypeScriptModule('src/lib/client-error-reporting.ts', {
		viteApiBase: API_BASE
	});
});

afterEach(() => {
	installBrowserGlobals();
});

after(() => {
	globalThis.fetch = originalFetch;
});

function waitFor(predicate, label, attempts = 80) {
	return new Promise((resolve, reject) => {
		let left = attempts;
		const tick = () => {
			if (predicate()) return resolve();
			left -= 1;
			if (left <= 0) return reject(new Error(`timed out waiting for ${label}`));
			setTimeout(tick, 5);
		};
		tick();
	});
}

test('reportClientError POSTs payload fields to client-errors', async () => {
	let request;
	let body;
	globalThis.fetch = async (input) => {
		request = input;
		body = await input.clone().json();
		return new Response(JSON.stringify({ event_id: 'e1', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	reporting.reportClientError(new Error('boom-visible'), { source: 'unit' }, 'ui-error');
	await waitFor(() => body !== undefined, 'POST body');

	assert.equal(request.url, `${API_BASE}/api/v1/client-errors`);
	assert.equal(request.method, 'POST');
	assert.equal(body.kind, 'ui-error');
	assert.equal(body.message, 'boom-visible');
	assert.equal(typeof body.client_event_id, 'string');
	assert.ok(body.client_event_id.length > 0);
	if (typeof request.keepalive === 'boolean') {
		assert.equal(request.keepalive, true);
	}
	await waitFor(() => {
		const raw = store.getItem(QUEUE_KEY);
		return raw === null || raw === '[]';
	}, 'drain');
});

test('a non-2xx response leaves the item queued', async () => {
	let posts = 0;
	globalThis.fetch = async () => {
		posts += 1;
		return new Response(JSON.stringify({ detail: 'nope' }), {
			status: 500,
			statusText: 'Server Error',
			headers: { 'content-type': 'application/json' }
		});
	};

	reporting.reportClientError(new Error('keep-me'), { source: 'unit-non2xx' }, 'ui-error');
	await waitFor(() => posts >= 1, 'failed POST');
	await new Promise((resolve) => setTimeout(resolve, 20));

	const raw = store.getItem(QUEUE_KEY);
	assert.ok(raw !== null, 'queue should remain');
	const queue = JSON.parse(raw);
	assert.equal(queue.length, 1);
	assert.equal(queue[0].message, 'keep-me');
});

test('a 2xx response drains the queued item', async () => {
	let posts = 0;
	globalThis.fetch = async () => {
		posts += 1;
		return new Response(JSON.stringify({ event_id: 'e2', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	reporting.reportClientError(new Error('drain-me'), { source: 'unit-2xx' }, 'ui-error');
	await waitFor(() => posts >= 1, 'success POST');
	await waitFor(() => {
		const raw = store.getItem(QUEUE_KEY);
		if (raw === null) return true;
		return JSON.parse(raw).length === 0;
	}, 'empty queue');
});
