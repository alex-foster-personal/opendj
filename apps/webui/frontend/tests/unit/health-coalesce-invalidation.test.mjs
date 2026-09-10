/**
 * PR #1656 review thread 2 (src/lib/api.ts:450) - a cached health read must
 * not outlive the question it answered.
 *
 * getHealth() shares one coalesced read across every boot-time caller for
 * BOOT_COALESCE_TTL_MS (src/lib/api/request-coalescer.ts). If a track is
 * imported during that window, BrowserPanel's own health read (getHealth()
 * without fresh:true) can be handed the answer from BEFORE the import for up
 * to the full TTL, feeding a stale (possibly zero) track count into
 * resolveBootPlaylist and leaving the browser pane blank even though the very
 * next request would show the real library.
 *
 * getHealth and the WS invalidation bus are bundled together in ONE esbuild
 * graph (tests/unit/fixtures/health-coalesce-entry.ts) -- see that file's
 * docstring for why a shared graph is required here, the same reason
 * daemon-capability-entry.ts and perf-ipc-entry.ts already exist.
 *
 * Each test loads its own fresh copy of that graph: api.ts's coalescer has no
 * `_resetForTests` hook, so sharing one load across tests would let an
 * earlier test's cached health entry leak into the next.
 *
 * [if] a 'library.changed' kind=tracks frame arrives inside the health TTL
 *   [then ⛔] the next getHealth() call re-fetches instead of serving the
 *   pre-change body
 * [if] the bus fires a resync (a seq gap) inside the TTL [then ⛔] the next
 *   getHealth() call re-fetches too -- a gap means health may have changed
 *   too and there is no way to know without asking again
 * [if] nothing invalidated the entry [then ⛔] concurrent callers still
 *   share ONE request -- the boot-burst dedup this PR ships must not regress
 *
 * Two more gaps found in the follow-up review round on this same PR (both
 * BLOCKING, both against the fix above):
 *
 * [if] a tracks change lands before the bus's FIRST-EVER successful
 *   connection [then ⛔] the next getHealth() call still re-fetches --
 *   events-bus.ts's own `_hasConnected` guard means `subscribeResync` never
 *   fires on that first connect (only on a reconnect), so the fix above alone
 *   leaves this window open: a change between module load and the bus's
 *   first `open` would otherwise ride through on the cached snapshot forever
 * [if] the underlying health fetch never settles (a stalled connection)
 *   [then ⛔] it must give up on its own after a bounded timeout, so a
 *   caller who arrives after the stall gets a FRESH request rather than
 *   joining a promise that will never resolve
 */
import assert from 'node:assert/strict';
import { afterEach, mock, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** Minimal stand-in for the browser WebSocket, matching events-bus.test.mjs. */
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
	deliver(topic, seq, payload) {
		this.onmessage?.({
			data: JSON.stringify({ topic, seq, ts: '2026-09-10T00:00:00.000Z', payload })
		});
	}
	close() {}
}

const NOOP_SCHEDULER = { setTimeout: () => 0, clearTimeout: () => {} };
const API_BASE = 'https://health-coalesce.example.test';

function healthResponse(trackCount) {
	return new Response(
		JSON.stringify({
			status: 'ok',
			version: '1',
			state_db: { path: '/state.db', tracks: trackCount, playlists: 0, pairings: 0 },
			waveform_materialization: { cache_entries: 0, cache_bytes: 0 }
		}),
		{ status: 200, headers: { 'content-type': 'application/json' } }
	);
}

let originalFetch;

afterEach(() => {
	if (originalFetch !== undefined) globalThis.fetch = originalFetch;
});

/** A fresh, fully isolated copy of the api.ts + events-bus.ts graph. */
async function loadGraph() {
	return loadTypeScriptModule('tests/unit/fixtures/health-coalesce-entry.ts', { viteApiBase: API_BASE });
}

/** Open the bus on the graph's own events-bus instance and deliver hello. */
function connectAndHello(mod) {
	let socket;
	mod.eventsBus.connect('ws://health-coalesce.example.test/api/v1/events', {
		socketFactory: (url) => {
			socket = new FakeSocket(url);
			return socket;
		},
		scheduler: NOOP_SCHEDULER
	});
	socket.open();
	socket.deliver('hello', 0, {
		contract_rev: 'rev-1',
		engine_version: '1.0.0',
		seq_start: 0,
		topics: []
	});
	return socket;
}

test('a tracks change inside the TTL invalidates the cached health read', async () => {
	const mod = await loadGraph();
	let calls = 0;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		calls += 1;
		return healthResponse(calls === 1 ? 0 : 5);
	};

	const first = await mod.getHealth();
	assert.equal(first.health.state_db.tracks, 0);
	assert.equal(calls, 1);

	const cached = await mod.getHealth();
	assert.equal(calls, 1, 'still inside the TTL: must be served from cache, not re-fetched');
	assert.equal(cached.health.state_db.tracks, 0);

	const socket = connectAndHello(mod);
	socket.deliver('library.changed', 1, { kind: 'tracks', ids: ['t-new'] });

	const afterImport = await mod.getHealth();
	assert.equal(calls, 2, 'a tracks change must invalidate the cached entry, forcing a re-fetch');
	assert.equal(afterImport.health.state_db.tracks, 5, 'the boot pane must see the post-import count');
});

test('a resync (seq gap) inside the TTL invalidates the cached health read too', async () => {
	const mod = await loadGraph();
	let calls = 0;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		calls += 1;
		return healthResponse(calls === 1 ? 0 : 5);
	};

	await mod.getHealth();
	assert.equal(calls, 1);

	connectAndHello(mod).deliver('jobs.updated', 5, {}); // seq_start 0 -> 5 is a gap.

	await mod.getHealth();
	assert.equal(calls, 2, 'a gap means health may have changed too, so it must not be served stale');
});

test('two callers within the TTL with no bus event still share ONE request', async () => {
	const mod = await loadGraph();
	let calls = 0;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		calls += 1;
		return healthResponse(8355);
	};

	const [a, b] = await Promise.all([mod.getHealth(), mod.getHealth()]);
	assert.equal(calls, 1, 'the boot-burst dedup this PR ships must not regress');
	assert.equal(a.health.state_db.tracks, 8355);
	assert.equal(b.health.state_db.tracks, 8355);
});

test('the FIRST-EVER bus connection also invalidates a cached health snapshot', async () => {
	const mod = await loadGraph();
	let calls = 0;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		calls += 1;
		return healthResponse(calls === 1 ? 0 : 5);
	};

	const first = await mod.getHealth();
	assert.equal(first.health.state_db.tracks, 0);
	assert.equal(calls, 1);

	// The bus has never connected before now, so events-bus.ts's own
	// `_hasConnected` guard means `subscribeResync` does NOT fire here (that
	// only fires on a RE-connect) even though a track change could have
	// landed in the window between the getHealth() above and this connect.
	connectAndHello(mod);

	const afterConnect = await mod.getHealth();
	assert.equal(calls, 2, 'the first-ever connection must invalidate too, not just a reconnect');
	assert.equal(afterConnect.health.state_db.tracks, 5);
});

test('a stalled initial health fetch is abandoned, not joined forever', async () => {
	const mod = await loadGraph();
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		let calls = 0;
		let capturedSignal;
		originalFetch = globalThis.fetch;
		globalThis.fetch = (request) => {
			// openapi-fetch calls `fetch(request, requestInitExt)` with the signal
			// already attached to the `Request` object, not on the second arg.
			calls += 1;
			if (calls === 1) {
				capturedSignal = request.signal;
				return new Promise((_resolve, reject) => {
					capturedSignal.addEventListener('abort', () => reject(capturedSignal.reason));
				});
			}
			return Promise.resolve(healthResponse(5));
		};

		const stalled = mod.getHealth();
		// Long enough to clear any sane fetch timeout without needing to know
		// its exact value here; mock timers make this instant, not a real wait.
		mock.timers.tick(3_600_000);
		await assert.rejects(stalled, /timed out/i);

		const after = await mod.getHealth();
		assert.equal(calls, 2, 'a caller after the stall settles must get a fresh request, not join it');
		assert.equal(after.health.state_db.tracks, 5);
	} finally {
		mock.timers.reset();
	}
});
