// requirement: a stem bundle the server is still pulling from R2 is not "no stems".
//
// On an installed spoke (the Air, silver) a track's stem bundle usually lives
// only in R2. The first GET /api/v1/tracks/{sid}/stems starts the download and
// answers HTTP 200 {status: 'unavailable', code: 'STEM_BUNDLE_HYDRATING',
// hydrating: true}. The deck used to read that as the settled "this track has
// no bundle", so stems appeared only when the track was loaded a second time.
//
// [if] the manifest GET answers STEM_BUNDLE_HYDRATING [then] probeStemArtifact
//   reports 'hydrating', never 'unavailable'
// [if] a later GET answers the manifest [then] awaitStemArtifact resolves ready
// [if] the GET answers STEM_BUNDLE_NOT_FOUND [then] it still settles as
//   'unavailable' at once (control: the ordinary no-stems track must not poll)
// [if] the deck goes stale while waiting [then] the wait returns null and
//   issues no further GET
// [if] the bundle is still hydrating past maxWaitMs [then] it rejects with a
//   message naming the wait, never resolving 'unavailable'
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://rekordbox-api.example.test';
let api;
let wait;
let originalFetch;

// Every body below is the real route's answer, captured into this fixture and
// re-checked against the live FastAPI route by
// tests/webui/test_stem_route_answers_fixture.py (so it cannot drift silently).
const ROUTE = JSON.parse(
	readFileSync(new URL('../fixtures/stem-route-answers.json', import.meta.url), 'utf8')
);
const MANIFEST = ROUTE.ready.body;
const HYDRATING = ROUTE.hydrating.body;
const NOT_FOUND = ROUTE.not_found.body;
const FAILED = ROUTE.hydration_failed;

/** Serve `bodies` in order, one per GET, repeating the last. */
function serve(bodies) {
	const calls = [];
	globalThis.fetch = async (input) => {
		calls.push(input instanceof Request ? input.url : String(input));
		const body = bodies[Math.min(calls.length - 1, bodies.length - 1)];
		return Response.json(body);
	};
	return calls;
}

/** A fake clock whose sleep advances time instantly. */
function fakeClock() {
	let t = 0;
	const slept = [];
	return {
		now: () => t,
		sleep: async (ms) => {
			slept.push(ms);
			t += ms;
		},
		slept
	};
}

before(async () => {
	api = await loadTypeScriptModule('src/lib/rb/api-rb.ts', { viteApiBase: API_BASE });
	wait = await loadTypeScriptModule('src/lib/rb/stem-hydrate-wait.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('a hydrating envelope probes as hydrating, not as a settled unavailable', async () => {
	serve([HYDRATING]);
	const probe = await api.probeStemArtifact('sid-1');
	assert.equal(probe.status, 'hydrating');
	assert.match(probe.error, /STEM_BUNDLE_HYDRATING/);
});

test('hydrating=true alone is enough, whatever the code says', async () => {
	serve([{ ...HYDRATING, code: 'SOMETHING_NEWER' }]);
	assert.equal((await api.probeStemArtifact('sid-1')).status, 'hydrating');
});

test('awaitStemArtifact waits through hydrating and lands the manifest', async () => {
	const calls = serve([HYDRATING, HYDRATING, MANIFEST]);
	const clock = fakeClock();
	const probe = await wait.awaitStemArtifact('sid-1', { now: clock.now, sleep: clock.sleep });
	assert.equal(probe.status, 'ready');
	assert.equal(probe.manifest.model, 'htdemucs');
	assert.equal(calls.length, 3, 'one GET per poll, stopping at the manifest');
	assert.deepEqual(clock.slept, [1000, 2000]);
});

test('control: a track with no bundle anywhere settles at once, with no polling', async () => {
	const calls = serve([NOT_FOUND, MANIFEST]);
	const clock = fakeClock();
	const probe = await wait.awaitStemArtifact('sid-1', { now: clock.now, sleep: clock.sleep });
	assert.deepEqual(probe, {
		status: 'unavailable',
		error: `STEM_BUNDLE_NOT_FOUND: ${NOT_FOUND.message}`
	});
	assert.equal(calls.length, 1);
	assert.deepEqual(clock.slept, []);
});

test('a deck that moves on stops the wait and asks nothing more', async () => {
	const calls = serve([HYDRATING]);
	const clock = fakeClock();
	let stale = false;
	const result = await wait.awaitStemArtifact('sid-1', {
		now: clock.now,
		sleep: async (ms) => {
			await clock.sleep(ms);
			stale = true;
		},
		isStale: () => stale
	});
	assert.equal(result, null);
	assert.equal(calls.length, 1);
});

test('a download that never finishes fails loud after maxWaitMs', async () => {
	const calls = serve([HYDRATING]);
	const clock = fakeClock();
	await assert.rejects(
		wait.awaitStemArtifact('sid-1', { now: clock.now, sleep: clock.sleep, maxWaitMs: 20_000 }),
		/stem bundle still downloading after 20 s/
	);
	// Backoff 1+2+3+5+5+4(clamped to the deadline) = 20 s, then one last look.
	assert.deepEqual(clock.slept, [1000, 2000, 3000, 5000, 5000, 4000]);
	assert.equal(calls.length, 7);
});

test('a server error while hydrating still rejects instead of settling empty', async () => {
	let n = 0;
	globalThis.fetch = async () => {
		n += 1;
		if (n === 1) return Response.json(HYDRATING);
		return Response.json(FAILED.body, { status: FAILED.status });
	};
	const clock = fakeClock();
	await assert.rejects(
		wait.awaitStemArtifact('sid-1', { now: clock.now, sleep: clock.sleep }),
		// The wait module loads its own copy of api-rb, so compare by shape,
		// not by `instanceof` against this file's copy of RbApiError.
		(error) => error?.status === 502 && error?.code === 'STEM_BUNDLE_HYDRATION_FAILED'
	);
});
