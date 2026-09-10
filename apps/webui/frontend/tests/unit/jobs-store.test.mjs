/**
 * Contract tests for the engine jobs store (src/lib/rb/jobs-store.svelte.ts).
 *
 * Two seams, no real IO: the bus is a fake object passed to attach() (the
 * store's documented injection point, mirroring how events-bus takes a socket
 * factory), and the "client" is the REAL generated client with globalThis.fetch
 * swapped, which is the idiom CONVERSION-PATTERN.md prescribes. Faking fetch
 * rather than the client means these tests exercise the actual URL building,
 * the throwing middleware and the error decoding, instead of a stub of them.
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { immediateBootScheduler, manualBootScheduler } from './fake-boot-scheduler.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://jobs.example.test';

let mod;
let store;
let originalFetch;
let originalConsoleError;
let errors;

/** One bus listener registry the test drives by hand. */
function makeFakeBus() {
	const topicListeners = new Map();
	const resyncListeners = new Set();
	return {
		bus: {
			subscribe(topic, listener) {
				let bucket = topicListeners.get(topic);
				if (bucket === undefined) {
					bucket = new Set();
					topicListeners.set(topic, bucket);
				}
				bucket.add(listener);
				return () => bucket.delete(listener);
			},
			subscribeResync(listener) {
				resyncListeners.add(listener);
				return () => resyncListeners.delete(listener);
			}
		},
		/** Deliver a jobs.updated frame carrying one job row. */
		deliver(payload, topic = 'jobs.updated') {
			const bucket = topicListeners.get(topic);
			if (bucket === undefined) return;
			for (const listener of [...bucket]) {
				listener({ topic, seq: 1, ts: '2026-08-19T10:00:00.000Z', payload });
			}
		},
		fireResync(reason = 'gap') {
			for (const listener of [...resyncListeners]) listener(reason);
		},
		topicCount(topic) {
			return topicListeners.get(topic)?.size ?? 0;
		},
		resyncCount() {
			return resyncListeners.size;
		}
	};
}

function job(overrides = {}) {
	return {
		id: 'job-1',
		kind: 'stems',
		payload: {},
		status: 'running',
		progress: 0.5,
		message: null,
		error: null,
		attempt: 1,
		created_at: '2026-08-19T10:00:00.000Z',
		started_at: '2026-08-19T10:00:01.000Z',
		finished_at: null,
		owner_pid: 4242,
		owner_boot_id: 'boot-1',
		worker_pid: null,
		worker_pgid: null,
		worker_argv: null,
		worker_started_at: null,
		external_ref: null,
		...overrides
	};
}

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

/** The engine's health body: legacy fields plus the three handshake ones. */
function engineHealth() {
	return {
		status: 'ok',
		state_db: {
			path: 'data/state/state.db',
			tracks: 1,
			playlists: 1,
			pairings: 0,
			last_writer_hostname: null,
			last_writer_at: null
		},
		cloud: { lock_holder: null },
		syncthing: null,
		bind_host: '127.0.0.1',
		version: '0.1.0',
		contract_rev: 'sha256:2f6c',
		engine_version: '0.1.0',
		boot_id: 'boot-1'
	};
}

before(async () => {
	// Loaded through the shared-graph entry so this store and the capability
	// probe it consults live in one bundle. Every call below is gated on the
	// serving daemon having a jobs API at all, so the suite first resolves the
	// probe against a real engine health body; daemon-capabilities.test.mjs
	// owns the legacy and not-yet-identified cases.
	mod = await loadTypeScriptModule('tests/unit/fixtures/daemon-capability-entry.ts', {
		viteApiBase: API_BASE
	});
	store = mod.jobsStore;
	originalFetch = globalThis.fetch;
	originalConsoleError = console.error;
	globalThis.fetch = async () => jsonResponse(engineHealth());
	assert.equal(await mod.capabilities.probe(), 'engine');
	globalThis.fetch = originalFetch;
});

after(() => {
	globalThis.fetch = originalFetch;
	console.error = originalConsoleError;
});

beforeEach(() => {
	store.detach();
	store.jobs = [];
	store.error = null;
	store.actionError = null;
	store.busyId = null;
	store.loading = false;
	store.drawerOpen = false;
	errors = [];
	console.error = (...args) => errors.push(args.map(String).join(' '));
});

// ------------------------------------------------------------------ hydrate

test('hydrate fills the list from the jobs endpoint, newest first', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse([
			job({ id: 'older', created_at: '2026-08-19T09:00:00.000Z' }),
			job({ id: 'newer', created_at: '2026-08-19T11:00:00.000Z' })
		]);
	};

	await store.hydrate();

	assert.equal(seen.url, `${API_BASE}/api/v1/jobs?limit=${mod.JOBS_LIST_LIMIT}`);
	assert.deepEqual(
		store.jobs.map((row) => row.id),
		['newer', 'older']
	);
	assert.equal(store.error, null);
	assert.equal(store.loading, false);
});

test('a failed hydrate records the error and KEEPS the rows already on screen', async () => {
	globalThis.fetch = async () => jsonResponse([job({ id: 'kept' })]);
	await store.hydrate();
	assert.deepEqual(
		store.jobs.map((row) => row.id),
		['kept']
	);

	globalThis.fetch = async () =>
		jsonResponse({ detail: { code: 'BOOM', message: 'jobs db is locked' } }, 500);
	await store.hydrate();

	assert.equal(store.error, 'jobs db is locked');
	assert.deepEqual(
		store.jobs.map((row) => row.id),
		['kept'],
		'a transient 500 must not read as "all your jobs vanished"'
	);
	assert.equal(store.loading, false);
});

// ------------------------------------------------------------------- upsert

test('a jobs.updated frame upserts its row by id instead of appending', async () => {
	const fake = makeFakeBus();
	globalThis.fetch = async () => jsonResponse([job({ id: 'job-1', progress: 0.1 })]);
	store.attach(fake.bus, immediateBootScheduler());
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(store.jobs.length, 1);

	fake.deliver(job({ id: 'job-1', progress: 0.6 }));
	fake.deliver(job({ id: 'job-1', progress: 0.9 }));

	assert.equal(store.jobs.length, 1, 'two frames for one id must not grow the list');
	assert.equal(store.jobs[0].progress, 0.9);
});

test('a frame for an unseen id is inserted in newest-first order', async () => {
	const fake = makeFakeBus();
	globalThis.fetch = async () =>
		jsonResponse([job({ id: 'first', created_at: '2026-08-19T09:00:00.000Z' })]);
	store.attach(fake.bus, immediateBootScheduler());
	await new Promise((resolve) => setImmediate(resolve));

	fake.deliver(job({ id: 'second', created_at: '2026-08-19T12:00:00.000Z' }));

	assert.deepEqual(
		store.jobs.map((row) => row.id),
		['second', 'first']
	);
});

test('a malformed frame is dropped and reported, never spread into the list', async () => {
	const fake = makeFakeBus();
	globalThis.fetch = async () => jsonResponse([]);
	store.attach(fake.bus, immediateBootScheduler());
	await new Promise((resolve) => setImmediate(resolve));

	fake.deliver({ kind: 'stems', status: 'running', progress: 0.5 });

	assert.equal(store.jobs.length, 0);
	assert.ok(
		errors.some((line) => line.includes('no string id')),
		'a frame with no id must be reported, not silently appended'
	);
});

// ------------------------------------------------------------------- resync

test('a resync refetches the whole list', async () => {
	const fake = makeFakeBus();
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse([job({ id: `after-${calls}` })]);
	};
	store.attach(fake.bus, immediateBootScheduler());
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(calls, 1, 'attach does the first fetch');

	fake.fireResync('gap');
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(calls, 2);
	assert.deepEqual(
		store.jobs.map((row) => row.id),
		['after-2']
	);
});

/**
 * PR #1656 review round 7 (P2 BLOCKING): events-bus.ts now fires a resync
 * (reason 'initial-connect') on the bus's first-ever open too, not just a
 * reconnect. This store already schedules its own deferred initial hydrate
 * through `scheduler.defer` below the PERF-R6 boot-window quiet period, so
 * hydrating again here on that same first open would duplicate the fetch and
 * bypass the window it exists to enforce.
 */
test('an initial-connect resync does not duplicate the deferred boot hydrate', async () => {
	const fake = makeFakeBus();
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse([job({ id: `after-${calls}` })]);
	};
	store.attach(fake.bus, immediateBootScheduler());
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(calls, 1, 'attach does the first, deferred fetch');

	fake.fireResync('initial-connect');
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(
		calls,
		1,
		"the bus's first-ever open must not duplicate the deferred boot hydrate this store already schedules"
	);
});

test('attach is idempotent, so a remount does not double every upsert', async () => {
	const fake = makeFakeBus();
	globalThis.fetch = async () => jsonResponse([]);

	store.attach(fake.bus, immediateBootScheduler());
	store.attach(fake.bus, immediateBootScheduler());

	assert.equal(fake.topicCount('jobs.updated'), 1);
	assert.equal(fake.resyncCount(), 1);

	store.detach();
	assert.equal(fake.topicCount('jobs.updated'), 0);
	assert.equal(fake.resyncCount(), 0);
});

test('attach subscribes at once but defers the catch-up fetch out of the boot burst', async () => {
	// PERF-R6. The 200-row list is nobody's critical path at second zero and
	// it was in the burst a startup deck load has to fight; the SUBSCRIPTION
	// is, because a jobs.updated frame that arrives during boot must not be
	// missed. So the two halves of attach() are split.
	// [if the subscription waits too then a frame during boot is lost]
	// [if the fetch does not wait then the burst is unchanged]
	const fake = makeFakeBus();
	const manual = manualBootScheduler();
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse([job({ id: 'job-1' })]);
	};

	store.attach(fake.bus, manual.scheduler);
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(fake.topicCount('jobs.updated'), 1, 'the bus is live from the first frame');
	assert.equal(calls, 0, 'but the catch-up list waits for the boot window');
	assert.equal(manual.pending(), 1, 'queued, never dropped');

	// A frame arriving before the fetch still lands, which is the whole
	// reason the subscription is not deferred with it.
	fake.deliver(job({ id: 'live-frame' }));
	assert.deepEqual(
		store.jobs.map((row) => row.id),
		['live-frame']
	);

	manual.release();
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(calls, 1, 'and the list is fetched once the window closes');
});

// ------------------------------------------------------------------ actions

test('cancel posts to the cancel route and upserts the returned row', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse(job({ id: 'job-1', status: 'cancelling' }));
	};
	store.jobs = [job({ id: 'job-1', status: 'running' })];

	await store.cancel('job-1');

	assert.equal(seen.url, `${API_BASE}/api/v1/jobs/job-1/cancel`);
	assert.equal(seen.method, 'POST');
	assert.equal(store.jobs[0].status, 'cancelling');
	assert.equal(store.actionError, null);
	assert.equal(store.busyId, null);
});

test('a 409 refusal is surfaced verbatim, not reworded or swallowed', async () => {
	const refusal = 'job job-1 is queued; only a running job cancels';
	globalThis.fetch = async () => jsonResponse({ detail: { code: 'CONFLICT', message: refusal } }, 409);
	store.jobs = [job({ id: 'job-1', status: 'queued' })];

	await store.cancel('job-1');

	assert.deepEqual(store.actionError, { id: 'job-1', message: refusal });
	assert.equal(store.jobs[0].status, 'queued', 'a refused cancel must not move the row');
	assert.equal(store.busyId, null, 'the row must not stay stuck busy after a refusal');
});

test('reenqueue posts to the reenqueue route and upserts the requeued row', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse(job({ id: 'job-1', status: 'queued', attempt: 2, progress: 0 }));
	};
	store.jobs = [job({ id: 'job-1', status: 'failed', progress: 0.4 })];

	await store.reenqueue('job-1');

	assert.equal(seen.url, `${API_BASE}/api/v1/jobs/job-1/reenqueue`);
	assert.equal(store.jobs[0].status, 'queued');
	assert.equal(store.jobs[0].attempt, 2);
});

// --------------------------------------------------------------- predicates

test('only a running job offers cancel, and queued says why not', () => {
	assert.equal(mod.canCancel({ status: 'running' }), true);
	assert.equal(mod.canCancel({ status: 'queued' }), false);
	assert.equal(mod.canCancel({ status: 'cancelling' }), false);
	assert.equal(mod.canCancel({ status: 'succeeded' }), false);
	assert.match(mod.cancelRefusal({ status: 'queued' }), /only cancels a job once a worker owns it/);
	assert.equal(mod.cancelRefusal({ status: 'running' }), null);
});

test('only a terminal job offers re-enqueue', () => {
	for (const status of ['succeeded', 'failed', 'cancelled', 'unknown']) {
		assert.equal(mod.canReenqueue({ status }), true, `${status} should re-enqueue`);
	}
	for (const status of ['queued', 'running', 'cancelling']) {
		assert.equal(mod.canReenqueue({ status }), false, `${status} should not re-enqueue`);
		assert.match(mod.reenqueueRefusal({ status }), /only a finished job re-enqueues/);
	}
});

// ------------------------------------------------------------------ display

test('progress renders as a clamped whole percent', () => {
	assert.equal(mod.progressPct(0), 0);
	assert.equal(mod.progressPct(0.456), 46);
	assert.equal(mod.progressPct(1), 100);
	assert.equal(mod.progressPct(1.5), 100, 'a server overshoot must not paint past the bar');
	assert.equal(mod.progressPct(-1), 0);
	assert.equal(mod.progressPct(Number.NaN), 0);
});

test('the error tail keeps the last lines and drops blank ones', () => {
	assert.equal(mod.errorTail(null), '');
	assert.equal(mod.errorTail(undefined), '');
	assert.equal(mod.errorTail('one\n\ntwo\nthree\nfour', 2), 'three\nfour');
	assert.equal(mod.errorTail('only one'), 'only one');
});

test('a null timestamp renders blank rather than Invalid Date', () => {
	assert.equal(mod.formatJobTime(null), '');
	assert.equal(mod.formatJobTime(''), '');
	assert.equal(mod.formatJobTime('not a date'), '');
	assert.notEqual(mod.formatJobTime('2026-08-19T10:00:00.000Z'), '');
});

test('the drawer toggle returns to its initial state after two toggles', () => {
	assert.equal(store.drawerOpen, false);
	mod.toggleJobsDrawer();
	assert.equal(store.drawerOpen, true);
	mod.toggleJobsDrawer();
	assert.equal(store.drawerOpen, false);
});
