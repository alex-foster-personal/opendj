/**
 * Capability gating: which daemon is serving, and what stays inert.
 *
 * The SPA is served by two daemons with different APIs (legacy has
 * /api/v1/progress and no jobs; the engine has jobs + the events socket and
 * drops progress). One probe of /api/v1/health decides which, and every
 * daemon-specific surface reads that answer instead of discovering it by
 * 404ing.
 *
 * No stubs beyond globalThis.fetch: the probe, the jobs store and the progress
 * fetcher are the real modules, sharing one bundle (fixtures/
 * daemon-capability-entry.ts) so they share one capability store.
 *
 * Regression lines:
 * - if a health body with all three handshake fields is not read as 'engine'
 *   then the discriminator has drifted from apps/engine_core/app.py
 * - if a legacy health body grants the jobs capability then the drawer 404s again
 * - if hydrate() or attach() issues a request while the daemon has no jobs API
 *   then the whole gate is decorative
 * - if fetchProgress issues a request against the engine then /progress-tree is
 *   back to retrying a 404 every 30 seconds
 * - if an unresolved or failed probe grants ANY capability then the UI is
 *   guessing which daemon it is talking to
 * - if a second probe() re-requests after a success then it is a poll, not a probe
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://capability.example.test';

let mod;
let caps;
let originalFetch;
let originalConsoleError;
/** Every URL fetch was asked for since the last reset. */
let requested;

/** The legacy daemon's /api/v1/health body (apps/webui/server/models.py). */
function legacyHealth(overrides = {}) {
	return {
		status: 'ok',
		state_db: {
			path: 'data/state/state.db',
			tracks: 8355,
			playlists: 120,
			pairings: 3,
			last_writer_hostname: null,
			last_writer_at: null
		},
		cloud: { lock_holder: null },
		syncthing: null,
		bind_host: '127.0.0.1',
		version: '0.1.0',
		...overrides
	};
}

/** The engine's body: the legacy one plus the three handshake fields. */
function engineHealth(overrides = {}) {
	return legacyHealth({
		contract_rev: 'sha256:2f6c',
		engine_version: '0.1.0',
		boot_id: 'boot-1',
		...overrides
	});
}

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

/** Answer every request with one body, recording what was asked for. */
function serve(body, status = 200) {
	globalThis.fetch = async (request) => {
		requested.push(request.url);
		return jsonResponse(body, status);
	};
}

/** Refuse every request, so a test can prove nothing was even attempted. */
function serveNothing() {
	globalThis.fetch = async (request) => {
		requested.push(request.url);
		throw new Error(`no request was expected, got ${request.url}`);
	};
}

before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/daemon-capability-entry.ts', {
		viteApiBase: API_BASE
	});
	caps = mod.capabilities;
	originalFetch = globalThis.fetch;
	originalConsoleError = console.error;
});

after(() => {
	globalThis.fetch = originalFetch;
	console.error = originalConsoleError;
});

beforeEach(() => {
	caps._resetForTests();
	mod.jobsStore.detach();
	mod.jobsStore.jobs = [];
	mod.jobsStore.error = null;
	requested = [];
	console.error = () => undefined;
});

// --------------------------------------------------------------- the probe

test('an engine health body resolves the engine capabilities', async () => {
	serve(engineHealth());

	assert.equal(await caps.probe(), 'engine');
	assert.deepEqual(requested, [`${API_BASE}/api/v1/health`]);
	assert.equal(caps.jobs, true);
	assert.equal(caps.events, true);
	assert.equal(caps.progressLedger, false);
	assert.equal(caps.error, null);
	assert.equal(mod.jobsRefusal(), null);
	assert.match(mod.progressRefusal(), /progress ledger not offered by this daemon/);
	assert.equal(mod.eventsRefusal(), null);
});

test('a legacy health body resolves the legacy capabilities', async () => {
	serve(legacyHealth());

	assert.equal(await caps.probe(), 'legacy');
	assert.equal(caps.jobs, false);
	assert.equal(caps.events, false);
	assert.equal(caps.progressLedger, true);
	assert.match(mod.jobsRefusal(), /jobs API not offered by this daemon/);
	assert.equal(mod.progressRefusal(), null);
	assert.match(mod.eventsRefusal(), /event bus not offered by this daemon/);
});

test('the probe reads the bytes, not the generated type', async () => {
	// api-types.ts types /api/v1/health as EngineHealthOut because openapi.json
	// is the ENGINE contract. A legacy body therefore type-checks as an engine
	// one; only a runtime field check can tell them apart.
	serve(legacyHealth({ contract_rev: '' }));
	assert.equal(await caps.probe(), 'legacy', 'an empty handshake string is not a handshake');
});

test('a HALF-present handshake is an explicit failure, never a guess', async () => {
	serve(legacyHealth({ engine_version: '0.1.0' }));

	assert.equal(await caps.probe(), 'unknown');
	assert.match(caps.error, /carries engine_version but not contract_rev, boot_id/);
	assert.equal(caps.jobs, false);
	assert.equal(caps.progressLedger, false);
});

test('a non-object health body leaves every capability off', async () => {
	serve([]);

	assert.equal(await caps.probe(), 'unknown');
	assert.match(caps.error, /answered an array, expected a JSON object/);
	assert.equal(caps.jobs, false);
	assert.equal(caps.events, false);
	assert.equal(caps.progressLedger, false);
});

test('a daemon that is down leaves the flavor unknown and every surface inert', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};

	assert.equal(await caps.probe(), 'unknown');
	assert.match(caps.error, /fetch failed/);
	assert.equal(caps.jobs, false);
	assert.equal(caps.events, false);
	assert.equal(caps.progressLedger, false);
	assert.match(mod.jobsRefusal(), /daemon not identified yet/);
	assert.match(mod.progressRefusal(), /daemon not identified yet/);
});

test('a successful probe is memoized: one request however often it is asked', async () => {
	serve(engineHealth());
	await caps.probe();
	await caps.probe();
	await Promise.all([caps.probe(), caps.probe()]);

	assert.deepEqual(requested, [`${API_BASE}/api/v1/health`]);
});

test('a FAILED probe is not memoized, so a daemon that comes up is picked up', async () => {
	globalThis.fetch = async (request) => {
		requested.push(request.url);
		throw new TypeError('fetch failed');
	};
	assert.equal(await caps.probe(), 'unknown');

	serve(legacyHealth());
	assert.equal(await caps.probe(), 'legacy');
	assert.equal(requested.length, 2, 'the second probe must actually retry');
});

test('concurrent probes share one in-flight request', async () => {
	serve(engineHealth());
	const [a, b] = await Promise.all([caps.probe(), caps.probe()]);

	assert.equal(a, 'engine');
	assert.equal(b, 'engine');
	assert.equal(requested.length, 1);
});

// ------------------------------------------------------- the jobs surface

test('the jobs store fires NOTHING under legacy capabilities', async () => {
	serve(legacyHealth());
	await caps.probe();
	requested = [];
	serveNothing();

	await mod.jobsStore.hydrate();

	assert.deepEqual(requested, [], 'a legacy boot has no /api/v1/jobs to ask');
	assert.match(mod.jobsStore.error, /jobs API not offered by this daemon/);
	assert.deepEqual(mod.jobsStore.jobs, []);
});

test('attach() under legacy capabilities subscribes to nothing and fetches nothing', async () => {
	serve(legacyHealth());
	await caps.probe();
	requested = [];
	serveNothing();

	let subscribed = 0;
	const detach = mod.jobsStore.attach({
		subscribe: () => {
			subscribed += 1;
			return () => undefined;
		},
		subscribeResync: () => {
			subscribed += 1;
			return () => undefined;
		}
	});

	assert.equal(subscribed, 0, 'a daemon with no jobs API has no jobs.updated topic');
	assert.deepEqual(requested, []);
	assert.match(mod.jobsStore.error, /jobs API not offered by this daemon/);
	assert.doesNotThrow(detach);
});

test('the jobs store fires nothing before the probe has answered', async () => {
	serveNothing();

	await mod.jobsStore.hydrate();

	assert.deepEqual(requested, [], 'an unidentified daemon is not assumed to be the engine');
	assert.match(mod.jobsStore.error, /daemon not identified yet/);
});

test('the jobs store DOES fetch once the engine is identified', async () => {
	serve(engineHealth());
	await caps.probe();
	requested = [];
	serve([]);

	await mod.jobsStore.hydrate();

	assert.deepEqual(requested, [`${API_BASE}/api/v1/jobs?limit=${mod.JOBS_LIST_LIMIT}`]);
	assert.equal(mod.jobsStore.error, null);
});

// --------------------------------------------------- the progress surface

test('fetchProgress fires NOTHING under engine capabilities', async () => {
	serve(engineHealth());
	await caps.probe();
	requested = [];
	serveNothing();

	await assert.rejects(mod.fetchProgress(), /not attempted: progress ledger not offered/);
	assert.deepEqual(requested, [], 'the engine drops the progress router; asking it is pointless');
});

test('fetchProgress fires nothing before the probe has answered', async () => {
	serveNothing();

	await assert.rejects(mod.fetchProgress(), /not attempted: daemon not identified yet/);
	assert.deepEqual(requested, []);
});

test('fetchProgress DOES fetch once the legacy daemon is identified', async () => {
	serve(legacyHealth());
	await caps.probe();
	requested = [];
	globalThis.fetch = async (request) => {
		requested.push(request.url);
		return jsonResponse({
			meta: { branch: 'master', updated: '2026-08-19', convention: 'test' },
			areas: [],
			file_git: { last_sha: null, last_author: null, last_date: null }
		});
	};

	const progress = await mod.fetchProgress();

	assert.deepEqual(requested, [`${API_BASE}/api/v1/progress`]);
	assert.equal(progress.meta.branch, 'master');
});
