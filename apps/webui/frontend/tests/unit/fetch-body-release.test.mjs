/**
 * P0 memory leak, Open DJ 1.0 alpha (Tue 6 Oct 2026): every fetch whose body
 * the page never reads must release it.
 *
 * WHY THIS FILE EXISTS. Packaged build 17 on WKWebView grew 27 MB/min with
 * every deck paused (84 MB/min with four decks loaded), and a heap snapshot
 * with EMPTY decks held 2,243 live fetch Response objects and 2,951
 * AbortSignals. The 1 Hz UI-mirror PUT never read or cancelled an accepted
 * answer's body, so WebKit kept each Response, its Request, the Request's
 * signal and the serialized mirror document alive: one leak per second, sized
 * by deck state.
 *
 * [if] an accepted mirror PUT's body is left open [then ⛔] the WebContent
 *   process leaks one Response per second for the life of /performance.
 * [if] a non-2xx lease read or a 409 / non-2xx order claim leaves its body
 *   open [then ⛔] every retry of an outage leaks one more.
 * [if] the result POST's answer is left open [then ⛔] every agent order leaks.
 * [if] a held long-poll answer (200 null, wait header) goes back to the claim
 *   without its body read [then ⛔] the idle poll leaks one every 20 s (Sol round 4).
 * [if] a 409 mirror PUT (lease_held, stale_snapshot, any other refusal) leaves
 *   its body open [then ⛔] every lease conflict leaks one (Sol review on #5703).
 * [if] releaseBody cancels a body a reader already holds [then ⛔] it breaks
 *   that reader (control: a locked or used body is left alone).
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { releaseBody, releaseBeaconBody } = await loadTypeScriptModule('src/lib/api/release-body.ts');
const { createTabLeadership } = await loadTypeScriptModule('src/lib/rb/tab-leadership.ts');
const { createLeasedMirrorPublisher, MIRROR_PATH, LEASE_PATH } = await loadTypeScriptModule(
	'src/lib/rb/leased-mirror-publisher.ts'
);
const { pollAgentOrders } = await loadTypeScriptModule('src/lib/rb/agent-orders.ts');

/** Every Response handed to the code under test, with whether its body was released. */
let issued;
let realFetch;

/**
 * The synthetic body never closes by itself: highWaterMark 0 means nothing is
 * pulled until a reader asks, and only that read (which delivers the bytes and
 * closes) or a cancel() marks the record released. An untouched body stays
 * open for the life of the test (Sol review round 3 on #5703).
 */
function tracked(status, payload, headers = {}) {
	const record = { status, released: false, response: null };
	const bytes = new TextEncoder().encode(JSON.stringify(payload));
	const stream = new ReadableStream(
		{
			pull(controller) {
				controller.enqueue(bytes);
				controller.close();
				record.released = true;
			},
			cancel() {
				record.released = true;
			}
		},
		{ highWaterMark: 0 }
	);
	const response = new Response(status === 204 ? null : stream, {
		status,
		headers: { 'content-type': 'application/json', ...headers }
	});
	record.response = response;
	issued.push(record);
	return response;
}

/** Released means cancelled by the page or read by it; nothing else sets it. */
const unreleased = () => issued.filter((r) => !r.released);

const flush = async () => {
	for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

beforeEach(() => {
	issued = [];
	realFetch = globalThis.fetch;
});

afterEach(() => {
	globalThis.fetch = realFetch;
});

test('a leader publishing the mirror N times leaves no Response body open', async () => {
	globalThis.fetch = async (url, init = {}) => {
		if (url === LEASE_PATH) return tracked(200, { held: false, holder: null });
		if (url === MIRROR_PATH && init.method === 'PUT') return tracked(202, { accepted: true });
		throw new Error(`unexpected request ${init.method ?? 'GET'} ${url}`);
	};
	const mirror = createLeasedMirrorPublisher({
		leadership: createTabLeadership({ locks: null }),
		clientId: 'tab-a',
		build: () => ({ client_open: true, client_id: 'tab-a' }),
		isPlaying: () => false,
		isVisible: () => false
	});
	const ticks = 50;
	for (let i = 0; i < ticks; i += 1) {
		mirror.publish();
		await flush();
	}
	assert.equal(mirror.isRegistered(), true, 'the PUTs were accepted');
	assert.ok(issued.filter((r) => r.status === 202).length >= ticks - 1, 'one PUT per tick');
	assert.deepEqual(
		unreleased().map((r) => r.status),
		[],
		'every accepted PUT body was released'
	);
});

test('a 409 mirror PUT (lease_held, stale_snapshot, other) leaves no body open', async () => {
	const refusals = [
		{ reason: 'stale_snapshot' },
		{ reason: 'something_else' },
		// Last: a lease_held demotes this tab, so no PUT follows it.
		{ reason: 'lease_held', holder: 'tab-b' }
	];
	globalThis.fetch = async (url, init = {}) => {
		if (url === LEASE_PATH) return tracked(200, { held: false, holder: null });
		if (url === MIRROR_PATH && init.method === 'PUT') {
			const refusal = refusals.shift();
			return refusal === undefined ? tracked(202, { accepted: true }) : tracked(409, refusal);
		}
		throw new Error(`unexpected request ${init.method ?? 'GET'} ${url}`);
	};
	const mirror = createLeasedMirrorPublisher({
		leadership: createTabLeadership({ locks: null }),
		clientId: 'tab-a',
		build: () => ({ client_open: true, client_id: 'tab-a' }),
		isPlaying: () => false,
		isVisible: () => false
	});
	const quiet = { info: console.info, error: console.error };
	console.info = console.error = () => {};
	try {
		for (let i = 0; i < 12; i += 1) {
			mirror.publish();
			await flush();
		}
	} finally {
		Object.assign(console, quiet);
	}
	assert.equal(issued.filter((r) => r.status === 409).length, 3, 'all three 409 refusals were exercised');
	assert.deepEqual(unreleased().map((r) => r.status), [], 'no 409 or 202 body left open');
});

test('a non-2xx lease read releases its body', async () => {
	globalThis.fetch = async (url) => {
		if (url === LEASE_PATH) return tracked(503, { detail: 'down' });
		throw new Error(`unexpected request ${url}`);
	};
	const mirror = createLeasedMirrorPublisher({
		leadership: createTabLeadership({ locks: null }),
		clientId: 'tab-a',
		build: () => ({}),
		isPlaying: () => false,
		isVisible: () => false
	});
	const realError = console.error;
	console.error = () => {};
	try {
		mirror.publish();
		await flush();
	} finally {
		console.error = realError;
	}
	assert.equal(issued.length, 1);
	assert.deepEqual(unreleased().map((r) => r.status), []);
});

test('the order poll releases 409, non-2xx and result-POST bodies', async () => {
	const claims = [
		() => tracked(409, { client_open: false }),
		() => tracked(500, { detail: 'boom' }),
		() => tracked(200, { id: 'o1', kind: 'sequence', payload: [] }),
		() => tracked(200, null, { 'x-opendj-order-wait-ms': '20000' })
	];
	let calls = 0;
	globalThis.fetch = async (url, init = {}) => {
		if (String(url).includes('/result')) return tracked(202, { ok: true });
		calls += 1;
		return (claims.shift() ?? (() => tracked(200, null, { 'x-opendj-order-wait-ms': '20000' })))();
	};
	const page = { isRegistered: () => true, whenRegistered: async () => {}, forget: () => {} };
	const quiet = { info: console.info, error: console.error, warn: console.warn };
	console.info = console.error = console.warn = () => {};
	try {
		await pollAgentOrders(page, () => {}, () => calls < 5);
	} finally {
		Object.assign(console, quiet);
	}
	assert.ok(issued.some((r) => r.status === 409));
	assert.ok(issued.some((r) => r.status === 500));
	assert.ok(issued.some((r) => r.status === 202), 'the result was posted');
	assert.deepEqual(unreleased().map((r) => r.status), []);
});

test('held long-poll answers (200 null + wait header) are each read before the next claim', async () => {
	let claims = 0;
	globalThis.fetch = async (url) => {
		if (String(url).includes('/result')) throw new Error('no order was claimed');
		claims += 1;
		return tracked(200, null, { 'x-opendj-order-wait-ms': '20000' });
	};
	const page = { isRegistered: () => true, whenRegistered: async () => {}, forget: () => {} };
	await pollAgentOrders(page, () => {}, () => claims < 25);
	assert.equal(issued.length, 25, 'every held answer went straight back to the long poll');
	assert.deepEqual(unreleased().map((r) => r.status), [], 'no held long-poll body left open');
});

test('control: a body nobody touches still reads as open', async () => {
	tracked(200, { a: 1 });
	const untouched = issued.at(-1);
	await flush();
	assert.equal(untouched.released, false, 'the synthetic stream does not release itself');
	assert.equal(untouched.response.bodyUsed, false);
	assert.deepEqual(unreleased().map((r) => r.status), [200], 'unreleased() reports it');
	await untouched.response.body.cancel();
});

test('control: releaseBody leaves a body its reader already holds alone', async () => {
	tracked(200, { a: 1 });
	const locked = issued.at(-1);
	const reader = locked.response.body.getReader();
	releaseBody(locked.response);
	assert.equal(locked.released, false, 'a locked body is not cancelled under its reader');
	reader.releaseLock();
	tracked(200, { a: 1 });
	const used = issued.at(-1);
	await used.response.json();
	assert.equal(used.released, true, 'a full read is what releases it');
	assert.doesNotThrow(() => releaseBody(used.response), 'a used body is left alone');
	tracked(200, { a: 1 });
	const open = issued.at(-1);
	releaseBody(open.response);
	await flush();
	assert.equal(open.released, true, 'an unread body is cancelled');
});

test('releaseBeaconBody reads a keepalive beacon body instead of cancelling it', async () => {
	// [if] heartbeat or client-samples still cancel an unfinished keepalive
	// body [then] Chromium reports net::ERR_ABORTED and smoke fails [else stop].
	let cancelled = false;
	let pulled = false;
	const stream = new ReadableStream({
		pull(controller) {
			pulled = true;
			controller.enqueue(new TextEncoder().encode('{}'));
			controller.close();
		},
		cancel() {
			cancelled = true;
		},
	});
	const response = new Response(stream, { status: 202 });
	releaseBeaconBody(response);
	// undici reads the body on a timer tick, not on setImmediate.
	await new Promise((resolve) => setTimeout(resolve, 50));
	assert.equal(cancelled, false);
	assert.equal(pulled, true);
	assert.equal(response.bodyUsed, true);
});
