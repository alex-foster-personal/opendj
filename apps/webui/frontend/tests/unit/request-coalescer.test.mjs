/**
 * One request per endpoint per boot (boot-burst de-duplication).
 *
 * What this guards: the /performance route was measured on Wed 9 Sep 2026
 * issuing GET /api/v1/health four times and GET /api/v1/ui-prefs twice
 * inside the first second, from six separate mount-time call sites. Each
 * caller needs the answer; none of them needs its own request. The module
 * under test is what makes those callers share one, and the two properties
 * that matter are opposites, so both are asserted here with a hand-driven
 * clock: concurrent callers MUST share, and a later caller MUST NOT be
 * handed a stale promise.
 *
 * Regression lines:
 *  - if two concurrent callers of one key run two requests then nothing was
 *    deduplicated and the boot burst is unchanged
 *  - if a caller inside the TTL re-requests then the two mount waves stay
 *    two requests and the measured duplicate survives
 *  - if a caller past the TTL reuses the settled promise then this is a
 *    cache with no invalidation and its first stale read is a bug
 *  - if a rejected call is retained then one failed probe fails every
 *    caller inside the window
 *  - if invalidate() does not drop the entry then a write cannot clear the
 *    read it just replaced
 *  - if two keys share one entry then a caller gets another endpoint's body
 *    typed as its own
 */
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

/** A hand-driven clock. Nothing here advances on its own. */
function makeHost() {
	let clock = 0;
	return {
		now: () => clock,
		advance(ms) {
			clock += ms;
		}
	};
}

/** A request that resolves only when the test says so, and counts calls. */
function makeDeferredRequest(value) {
	const state = { calls: 0, resolvers: [], rejecters: [] };
	const request = () => {
		state.calls += 1;
		return new Promise((resolve, reject) => {
			state.resolvers.push(() => resolve(value));
			state.rejecters.push(reject);
		});
	};
	state.request = request;
	state.resolveAll = () => {
		for (const r of state.resolvers.splice(0)) r();
		state.rejecters.length = 0;
	};
	state.rejectAll = (error) => {
		for (const r of state.rejecters.splice(0)) r(error);
		state.resolvers.length = 0;
	};
	return state;
}

/** Let already-resolved promise callbacks run before asserting on them. */
async function settle() {
	await Promise.resolve();
	await Promise.resolve();
}

before(async () => {
	mod = await loadTypeScriptModule('src/lib/api/request-coalescer.ts');
});

let host;
let coalescer;

beforeEach(() => {
	host = makeHost();
	coalescer = mod.createCoalescer(host);
});

test('two concurrent callers share ONE underlying request', async () => {
	const backing = makeDeferredRequest('health-body');

	const first = coalescer.share('health', 1000, backing.request);
	const second = coalescer.share('health', 1000, backing.request);

	assert.equal(backing.calls, 1, 'the second caller must not issue its own request');
	assert.equal(first, second, 'both callers must hold the same promise');

	backing.resolveAll();
	assert.deepEqual(await Promise.all([first, second]), ['health-body', 'health-body']);
	assert.equal(backing.calls, 1);
});

test('a caller inside the TTL of a SETTLED call reuses it', async () => {
	const backing = makeDeferredRequest('health-body');

	const first = coalescer.share('health', 1000, backing.request);
	backing.resolveAll();
	assert.equal(await first, 'health-body');
	await settle();

	// The two mount waves were 77-149ms apart when measured; this is that
	// gap, and it is the case an in-flight-only coalescer would miss.
	host.advance(150);
	const second = coalescer.share('health', 1000, backing.request);

	assert.equal(backing.calls, 1, 'the second wave must not re-request inside the window');
	assert.equal(await second, 'health-body');
});

test('a LATER, separate call does NOT reuse a stale promise', async () => {
	const backing = makeDeferredRequest('first-body');

	const first = coalescer.share('health', 1000, backing.request);
	backing.resolveAll();
	assert.equal(await first, 'first-body');
	await settle();

	host.advance(1001);
	const second = coalescer.share('health', 1000, backing.request);

	assert.equal(backing.calls, 2, 'past the TTL the coalescer must issue a fresh request');
	assert.notEqual(first, second, 'a past-TTL caller must not be handed the settled promise');
	backing.resolveAll();
	assert.equal(await second, 'first-body');
});

test('a rejected call is dropped, so the next caller retries', async () => {
	const backing = makeDeferredRequest('health-body');

	const failing = coalescer.share('health', 1000, backing.request);
	backing.rejectAll(new Error('daemon down'));
	await assert.rejects(failing, /daemon down/);
	await settle();

	const retry = coalescer.share('health', 1000, backing.request);
	assert.equal(backing.calls, 2, 'a failure must not be shared forward inside the window');
	backing.resolveAll();
	assert.equal(await retry, 'health-body');
});

test('invalidate() drops the entry so a write can clear its own read', async () => {
	const backing = makeDeferredRequest('prefs-body');

	const first = coalescer.share('ui-prefs', 1000, backing.request);
	backing.resolveAll();
	await first;
	await settle();

	coalescer.invalidate('ui-prefs');
	coalescer.share('ui-prefs', 1000, backing.request);

	assert.equal(backing.calls, 2, 'an invalidated key must re-request immediately');
});

test('distinct keys never share a request', async () => {
	const health = makeDeferredRequest('health-body');
	const prefs = makeDeferredRequest('prefs-body');

	const a = coalescer.share('health', 1000, health.request);
	const b = coalescer.share('ui-prefs', 1000, prefs.request);

	health.resolveAll();
	prefs.resolveAll();
	assert.equal(await a, 'health-body');
	assert.equal(await b, 'prefs-body');
});

test('the shipped TTL clears the measured wave gap and stays under the poll cadences', () => {
	// An invariant, not a recorded value: the window has to be wider than
	// the boot waves it merges and narrower than the shortest cadence that
	// legitimately re-reads a coalesced endpoint (2500ms library ping,
	// 30000ms health poll). Pinning the number instead would rot the moment
	// either cadence moved.
	const WIDEST_MEASURED_WAVE_GAP_MS = 149;
	const FASTEST_LEGITIMATE_REREAD_MS = 2_500;
	assert.ok(mod.BOOT_COALESCE_TTL_MS > WIDEST_MEASURED_WAVE_GAP_MS);
	assert.ok(mod.BOOT_COALESCE_TTL_MS < FASTEST_LEGITIMATE_REREAD_MS);
});
