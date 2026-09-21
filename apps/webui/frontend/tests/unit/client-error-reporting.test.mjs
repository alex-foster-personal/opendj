/**
 * Durable client-error queue after conversion onto the generated OpenAPI client.
 * 2xx drains; non-2xx leaves the head item queued for the next report.
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
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
	// UNIQUE PER CALL, as the real one is. A constant id would let a queue bug
	// that conflates two distinct reports pass unnoticed here.
	let uuid = 0;
	defineGlobal('crypto', {
		randomUUID: () => `aaaaaaaa-bbbb-cccc-dddd-${String(++uuid).padStart(12, '0')}`
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

test('any_deck_live carries the registered transport probe, null without one', async () => {
	// The engine holds the Sentry forward while this is true (the "never send
	// while a deck is live" rule). Three cases, each of which must be
	// distinguishable on the wire: no probe (null: the engine falls back to
	// its mirror), a probe saying live (true), a probe that throws (null, not
	// false: unknown must not read as "safe to send").
	const bodies = [];
	globalThis.fetch = async (input) => {
		bodies.push(await input.clone().json());
		return new Response(JSON.stringify({ event_id: 'e-live', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	reporting.setLiveTransportProbe(null);
	reporting.reportClientError(new Error('no-probe'), { source: 'live-a' }, 'ui-error');
	await waitFor(() => bodies.length === 1, 'the no-probe POST');
	assert.equal(bodies[0].any_deck_live, null);

	reporting.setLiveTransportProbe(() => true);
	reporting.reportClientError(new Error('live-probe'), { source: 'live-b' }, 'ui-error');
	await waitFor(() => bodies.length === 2, 'the live-probe POST');
	assert.equal(bodies[1].any_deck_live, true);

	reporting.setLiveTransportProbe(() => false);
	reporting.reportClientError(new Error('idle-probe'), { source: 'live-c' }, 'ui-error');
	await waitFor(() => bodies.length === 3, 'the idle-probe POST');
	assert.equal(bodies[2].any_deck_live, false);

	reporting.setLiveTransportProbe(() => {
		throw new Error('probe exploded');
	});
	reporting.reportClientError(new Error('throwing-probe'), { source: 'live-d' }, 'ui-error');
	await waitFor(() => bodies.length === 4, 'the throwing-probe POST');
	assert.equal(bodies[3].any_deck_live, null);
	reporting.setLiveTransportProbe(null);
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

/**
 * A REPORT RAISED WHILE A POST IS IN FLIGHT MUST SURVIVE.
 *
 * `flushQueue` used to take ONE snapshot of the queue before its first await
 * and write that snapshot back after each POST. `reportClientError` is
 * synchronous and appends straight to storage, so a second report landing
 * during the POST was written and then ERASED by the stale snapshot
 * overwriting it.
 *
 * That is the ordinary case, not a rare interleaving: an error toast writes its
 * own context-rich report, and anything it calls can report in the same tick.
 * The richer report is the one that arrives second, so the one lost was the one
 * worth having.
 *
 * NO PATCHED `fetch`. Unlike the three tests above, this one leaves
 * `globalThis.fetch` alone and stands up a REAL `node:http` server on
 * localhost, pointed at by the same `viteApiBase` the generated client reads.
 * The whole production request lifecycle therefore runs for real: the real
 * `api.POST`, a real fetch, a real socket, real headers, a real status and the
 * real response parse. Nothing about the client under test is substituted.
 *
 * That also makes the interleaving REAL rather than staged: the server accepts
 * the first request and does not answer it until the second report has been
 * written to storage, so the window under test is an actual in-flight HTTP
 * request rather than a promise a test held open.
 *
 * The server is a transport, not a stand-in implementation: it records what
 * arrived and returns the endpoint's real 200 shape. The client-error endpoint
 * itself is FastAPI and belongs to the python and e2e tiers; what is being
 * measured here is the browser-side durable queue's bookkeeping across an
 * await, which no server-side test can observe.
 *
 * [if] a report queued during an in-flight POST is dropped [then] fail, [else stop].
 */
test('a report raised during an in-flight POST is not erased by the flush', async () => {
	const posted = [];
	let releaseFirst;
	const firstHeld = new Promise((resolve) => {
		releaseFirst = resolve;
	});
	let received = 0;

	// The tests above replace `globalThis.fetch` and leave it replaced until
	// the `after` hook. Put the REAL one back, because a real server is only
	// worth standing up if a real request can reach it.
	const patchedFetch = globalThis.fetch;
	globalThis.fetch = originalFetch;

	const server = createServer((req, res) => {
		let body = '';
		req.on('data', (chunk) => {
			body += chunk;
		});
		req.on('end', async () => {
			received += 1;
			const isFirst = received === 1;
			// Hold the FIRST request open, unanswered, until the second report
			// has been written to storage. This is a genuinely in-flight HTTP
			// request, which is the state the defect lived in.
			if (isFirst) await firstHeld;
			posted.push({ url: req.url, method: req.method, message: JSON.parse(body).message });
			res.writeHead(200, { 'content-type': 'application/json' });
			res.end(JSON.stringify({ event_id: 'e-real', stored: true }));
		});
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const base = `http://127.0.0.1:${server.address().port}`;

	try {
		// A SECOND module instance, bound to the real server's origin. The one
		// loaded in `before` points at a host nothing listens on.
		const live = await loadTypeScriptModule('src/lib/client-error-reporting.ts', {
			viteApiBase: base
		});
		live.reportClientError(new Error('first-report'), { source: 'race-a' }, 'ui-error');
		await waitFor(() => received === 1, 'the first POST reaching the server', 400);
		// Synchronous, straight into storage, exactly as a toast's own report is.
		live.reportClientError(new Error('second-report'), { source: 'race-b' }, 'ui-error');
		releaseFirst();

		await waitFor(
			() => posted.some((p) => p.message === 'second-report'),
			'the second report reaching the server',
			400
		);
		assert.deepEqual(
			posted.map((p) => p.message),
			['first-report', 'second-report']
		);
		assert.deepEqual(
			posted.map((p) => `${p.method} ${p.url}`),
			['POST /api/v1/client-errors', 'POST /api/v1/client-errors']
		);
		await waitFor(
			() => {
				const raw = store.getItem(QUEUE_KEY);
				return raw === null || JSON.parse(raw).length === 0;
			},
			'drain',
			400
		);
	} finally {
		globalThis.fetch = patchedFetch;
		await new Promise((resolve) => server.close(resolve));
	}
});
