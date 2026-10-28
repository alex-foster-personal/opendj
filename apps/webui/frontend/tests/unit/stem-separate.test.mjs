// requirement STEM-77: "Separate this track" on a NO STEMS deck.
//
// [if] a stems.separate job for the track exists that this session did not
//   start (HTTP/agent path, another tab, before a reload) [then] job() finds
//   it from kind + payload.stable_ids
// [if] such a job is queued/running/cancelling [then] separate() refuses and
//   issues no POST, so a click cannot start a second concurrent job
// [if] a library-wide {scope: 'pending'} job is queued/running/cancelling
//   [then] it covers the track the same way, and job() and the query say so
// [if] that scope job had already finished before the track was looked at
//   [then] it does not block and does not claim the track (control)
// [if] the stems source check has not answered, or failed [then] separate()
//   refuses with the reason and issues no POST; after a successful check it
//   enqueues (control)
// [if] no job for the track exists [then] separate() still enqueues (control)
// [if] the engine refuses a duplicate with 409 stems_job_active (another tab
//   or agent won the race) [then] the returned job is adopted, not an error;
//   any other 409 stays an error (control)
// [if] a browser agent sends {type: 'stem_separate', deck} [then] it runs the
//   same enqueue, and query() exposes the job, pending start and errors
// [if] a job names another library's data_dir (a CLI run with --data-dir)
//   [then] it neither blocks nor claims the deck; a job naming this engine's
//   library (any spelling) or none at all still does (control)
// [if] the covering job ends in ANY state (a library job can fail after
//   writing this track's bundle) [then] the deck probes once through the
//   stem_load decision: a bundle that is there loads; with none the deck
//   stays NO STEMS and only then shows the failure (control); a job still
//   running is not probed, and an ended job is not probed twice (overshoot)
//
// THE NETWORK EDGE. The browser-to-engine HTTP call is a real process
// boundary, so globalThis.fetch is the one double here. It does not invent
// the engine's answers: every request is checked against, and every answer
// is taken from, fixtures/stems-separate-wire.json, which was captured from
// the real routes and is re-checked against them by
// (private path). What the double changes, and why:
// a POST answer gets a fresh id and created_at (the engine mints both) and
// the requested stable_ids in place of the captured track (the engine echoes
// the request's ids into its canonical payload, which that pytest asserts).
// The duplicate 409 keeps the captured envelope (status, code, message) and
// carries the test's active row as `job`, the row the engine would hand back.
// Rows placed in the jobs list with another status stand for the
// `jobs.updated` frames the engine publishes as a job moves on.
//
// THE DECK EDGE (probe tests). The real retryDeckStems decision runs; its
// StemRetryPort is the deck engine's audio side, which node cannot run, so
// `reload` is the one double there: it stands for the engine's stem probe
// answering "bundle present" (ready) or "no bundle" (still unavailable), the
// same seam tests/unit/stem-retry.test.mjs uses.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://stem-separate.example.test';
/** The engine's own library; its health names `<data_dir>/state/state.db`. */
const ENGINE_DATA_DIR = '/srv/opendj/data';
const WIRE = JSON.parse(readFileSync(new URL('./fixtures/stems-separate-wire.json', import.meta.url), 'utf8'));

let mod;
let originalFetch;
let posts;
let planReads;
let planFails;
/** How the engine answers a POST: 'created' (201), 'duplicate' (the captured
 * 409 carrying the active job), or 'conflict-other' (a 409 of another kind). */
let postAnswer;
/** For 'duplicate': the active job the engine hands back. */
let duplicateOf;

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

function engineHealth() {
	return {
		status: 'ok',
		state_db: {
			path: `${ENGINE_DATA_DIR}/state/state.db`,
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

let seq = 0;
/** A job row of the captured shape, as the engine would publish it later. */
function rowFrom(captured, { stableIds, status = 'queued', error = null, kind } = {}) {
	seq += 1;
	const payload = { ...captured.payload };
	if (stableIds !== undefined) payload.stable_ids = [...stableIds];
	return {
		...captured,
		id: `job-${seq}`,
		created_at: `2026-10-15T10:00:${String(seq).padStart(2, '0')}.000+00:00`,
		payload,
		status,
		error,
		...(kind === undefined ? {} : { kind })
	};
}
const trackRow = (stableId, opts = {}) => rowFrom(WIRE.enqueue_track.response, { ...opts, stableIds: [stableId] });
const scopeRow = (opts = {}) => rowFrom(WIRE.enqueue_scope.response, opts);

/** The engine edge: the captured routes, checked request by request. */
function serveEngine() {
	globalThis.fetch = async (request) => {
		const url = new URL(request.url);
		if (request.method === 'POST' && url.pathname === '/api/v1/jobs') {
			const body = JSON.parse(await request.clone().text());
			posts.push(body);
			if (body.payload.scope !== undefined) {
				// The library prompt's scope request (enqueueStemsForPending).
				assert.deepEqual(body, WIRE.enqueue_scope.request, 'the scope request the real route was proven to accept');
			} else {
				const sent = WIRE.enqueue_track.request;
				assert.deepEqual(
					body,
					{ ...sent, payload: { ...sent.payload, stable_ids: body.payload.stable_ids } },
					'the store must send the payload the real route was proven to accept'
				);
			}
			if (postAnswer === 'duplicate') {
				const captured = WIRE.enqueue_duplicate.response;
				return jsonResponse({ detail: { ...captured.detail, job: duplicateOf } }, WIRE.enqueue_duplicate.status);
			}
			if (postAnswer === 'conflict-other') {
				// Carries a job too, so only the CODE can tell it from the duplicate refusal.
				return jsonResponse({ detail: { code: 'some_other_conflict', message: 'not a duplicate', job: duplicateOf } }, 409);
			}
			return jsonResponse(rowFrom(WIRE.enqueue_track.response, { stableIds: body.payload.stable_ids }), 201);
		}
		if (request.method === 'GET' && url.pathname === WIRE.plan.request.path) {
			planReads += 1;
			assert.deepEqual(Object.fromEntries(url.searchParams), WIRE.plan.request.query);
			if (planFails) return jsonResponse({ detail: 'state.db missing: /data/state/state.db' }, 503);
			return jsonResponse(WIRE.plan.response);
		}
		return jsonResponse({ detail: `unexpected ${request.method} ${url.pathname}` }, 500);
	};
}

/** A completed, successful source check (a no-op while the last one is fresh). */
async function checked() {
	await mod.stemSeparate.ensureChecked();
	assert.equal(mod.stemSeparate.checking, false);
	assert.equal(mod.stemSeparate.refusal(), null);
}

before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/stem-separate-entry.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => jsonResponse(engineHealth());
	assert.equal(await mod.capabilities.probe(), 'engine');
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	mod.jobsStore.detach();
	mod.jobsStore.jobs = [];
	posts = [];
	planReads = 0;
	planFails = false;
	postAnswer = 'created';
	duplicateOf = null;
	serveEngine();
});

// Runs first on purpose: the store is a module singleton, and this is the one
// moment no source check has answered yet.
test('separate() refuses while the source check has not answered, then enqueues once it has', async () => {
	assert.equal(mod.stemSeparate.checking, true, 'precondition: nothing has been checked yet');
	assert.equal(mod.stemSeparate.refusal(), null, 'which is exactly why refusal() alone cannot gate a start');
	const early = await mod.stemSeparate.separate('track-early');
	assert.equal(early.ok, false);
	assert.match(early.reason, /source check .* has not finished/);
	assert.deepEqual(posts, [], 'no job before the check answers');
	assert.equal(mod.stemSeparate.enqueueError('track-early'), null, 'the button already says Checking...');

	await checked();
	assert.ok(planReads >= 1, 'the refusal itself asked for the check');
	const later = await mod.stemSeparate.separate('track-early');
	assert.equal(later.ok, true, 'control: after a successful check the same call enqueues');
	assert.equal(posts.length, 1);
});

test('a failed source check refuses with its reason; a later good check lets it start', async () => {
	planFails = true;
	await mod.stemSeparate.ensureChecked(Date.now() + 120_000);
	assert.match(mod.stemSeparate.refusal(), /stems plan could not be read/);
	const refused = await mod.stemSeparate.separate('track-check-failed');
	assert.equal(refused.ok, false);
	assert.match(refused.reason, /refusing to start separation: the stems plan could not be read/);
	assert.deepEqual(posts, []);

	planFails = false;
	await mod.stemSeparate.ensureChecked(Date.now() + 120_000);
	await checked();
	assert.equal((await mod.stemSeparate.separate('track-check-failed')).ok, true);
	assert.equal(posts.length, 1);
});

test('the captured route answers pass the frontend job-row decoder', () => {
	for (const row of [WIRE.enqueue_track.response, WIRE.enqueue_scope.response]) {
		assert.equal(mod.readJobPayload(row), row);
	}
	assert.equal(WIRE.enqueue_track.response.kind, mod.STEMS_JOB_KIND);
});

test('job() finds a separation this session never started, by kind and payload.stable_ids', () => {
	assert.equal(mod.stemSeparate.job('track-fresh'), null, 'no row, no job');
	const foreign = rowFrom(WIRE.enqueue_track.response, { stableIds: ['track-x', 'track-fresh'], status: 'running' });
	mod.jobsStore.upsert(foreign);
	assert.deepEqual(mod.stemSeparate.job('track-fresh'), { id: foreign.id, attempt: foreign.attempt, status: 'running', error: null, scope: 'track', confirmed: true });
});

test('job() picks the newest matching row and ignores other kinds and other tracks', () => {
	const older = trackRow('track-n', { status: 'failed', error: 'boom' });
	const otherKind = trackRow('track-n', { kind: 'analysis.beatgrid', status: 'running' });
	const otherTrack = trackRow('track-other', { status: 'running' });
	const newer = trackRow('track-n', { status: 'succeeded' });
	for (const row of [older, otherKind, otherTrack, newer]) mod.jobsStore.upsert(row);
	assert.equal(mod.stemSeparate.job('track-n').id, newer.id);
	mod.jobsStore.jobs = [older, otherKind, otherTrack];
	assert.deepEqual(mod.stemSeparate.job('track-n'), { id: older.id, attempt: older.attempt, status: 'failed', error: 'boom', scope: 'track', confirmed: true });
	mod.jobsStore.jobs = [otherKind, otherTrack];
	assert.equal(mod.stemSeparate.job('track-n'), null, 'another kind or another track is not this job');
});

// Codex 4284606879: a re-enqueue keeps the row's original created_at, so an
// active re-run can sit BELOW a newer attempt that already ended.
test('an active re-enqueued row wins over a newer ended row for the track', async () => {
	await checked();
	const rerun = trackRow('track-rerun', { status: 'queued' });
	const newerEnded = trackRow('track-rerun', { status: 'failed', error: 'oom' });
	mod.jobsStore.jobs = [newerEnded, rerun];
	assert.deepEqual(mod.stemSeparate.job('track-rerun'), { id: rerun.id, attempt: rerun.attempt, status: 'queued', error: null, scope: 'track', confirmed: true });
	const outcome = await mod.stemSeparate.separate('track-rerun');
	assert.equal(outcome.ok, false, 'the re-run is under way, so no second job');
	assert.deepEqual(posts, []);
	// Its end is what the deck then follows (and probes), not the older verdict.
	mod.jobsStore.jobs = [newerEnded, { ...rerun, status: 'succeeded' }];
	assert.equal(mod.stemSeparate.job('track-rerun').id, newerEnded.id, 'both ended: the newest row, as before');
});

test('an adopted job wins over a newer ended row; an active row wins over an adopted ended one', async () => {
	await checked();
	postAnswer = 'duplicate';
	const adopted = trackRow('track-adopt-order', { status: 'running' });
	duplicateOf = adopted;
	const newerEnded = { ...trackRow('track-adopt-order', { status: 'failed', error: 'oom' }), finished_at: '2026-10-15T11:00:00.000+00:00' };
	mod.jobsStore.jobs = [newerEnded];
	assert.equal((await mod.stemSeparate.separate('track-adopt-order')).adopted, true);
	const adoptedDone = { ...adopted, status: 'succeeded', finished_at: '2026-10-15T12:00:00.000+00:00' };
	mod.jobsStore.jobs = [newerEnded, adoptedDone];
	assert.equal(mod.stemSeparate.job('track-adopt-order').id, adopted.id, 'the job this tab adopted, which ended last, over older history');
	const other = trackRow('track-adopt-order', { status: 'running' });
	mod.jobsStore.jobs = [newerEnded, adoptedDone, other];
	assert.equal(mod.stemSeparate.job('track-adopt-order').id, other.id, 'a run under way beats the adopted one that ended');
});

// Codex 4286181055: this tab remembers J1 (ended); another tab then runs J2
// for the track. J2 wins while active, and must still win once it ends.
test('a later run from another tab is followed after it ends, not the remembered older job', async () => {
	await checked();
	const j1 = trackRow('track-j2', { status: 'queued' });
	postAnswer = 'duplicate';
	duplicateOf = j1;
	assert.equal((await mod.stemSeparate.separate('track-j2')).job_id, j1.id, 'this tab remembers J1');
	const j1Done = { ...j1, status: 'failed', error: 'oom', finished_at: '2026-10-15T11:00:00.000+00:00' };
	const j2 = trackRow('track-j2', { status: 'running' });
	mod.jobsStore.jobs = [j2, j1Done];
	assert.equal(mod.stemSeparate.job('track-j2').id, j2.id, 'J2 while it runs');
	const j2Done = { ...j2, status: 'succeeded', finished_at: '2026-10-15T12:00:00.000+00:00' };
	mod.jobsStore.jobs = [j2Done, j1Done];
	assert.deepEqual(mod.stemSeparate.job('track-j2'), { id: j2.id, attempt: j2.attempt, status: 'succeeded', error: null, scope: 'track', confirmed: true }, 'and once it ends, so the deck probes its stems');
	// Control (overshoot): the remembered job, when it is the one that ended last, is still followed.
	mod.jobsStore.jobs = [{ ...j2Done, finished_at: '2026-10-15T10:00:00.000+00:00' }, j1Done];
	assert.equal(mod.stemSeparate.job('track-j2').id, j1.id);
	// And on a tie the remembered job wins over list order.
	mod.jobsStore.jobs = [{ ...j2Done, finished_at: j1Done.finished_at }, j1Done];
	assert.equal(mod.stemSeparate.job('track-j2').id, j1.id);
});

test('control (overshoot): with no active or adopted row the newest ended row is still the answer', () => {
	const older = trackRow('track-history', { status: 'succeeded' });
	const newer = trackRow('track-history', { status: 'failed', error: 'boom' });
	mod.jobsStore.jobs = [newer, older];
	assert.equal(mod.stemSeparate.job('track-history').id, newer.id);
});

for (const status of ['queued', 'running', 'cancelling']) {
	test(`separate() refuses and sends no POST while a ${status} job for the track exists`, async () => {
		await checked();
		const stableId = `track-active-${status}`;
		const active = trackRow(stableId, { status });
		mod.jobsStore.upsert(active);
		const outcome = await mod.stemSeparate.separate(stableId);
		assert.equal(outcome.ok, false);
		assert.match(outcome.reason, new RegExp(`already ${status} \\(job ${active.id}\\)`));
		assert.deepEqual(posts, [], 'the engine does not dedupe, so no second job may be enqueued');
	});

	if (status !== 'queued') test(`a library-wide scope job seen queued for the track, now ${status}, covers it: no POST, and job() says scope pending`, async () => {
		await checked();
		const stableId = `track-scope-${status}`;
		const scope = scopeRow({ status: 'queued' });
		mod.jobsStore.upsert(scope);
		mod.stemSeparate.noteNoStems(stableId);
		assert.equal(mod.stemSeparate.job(stableId)?.id, scope.id, 'seen while queued: its worker has not taken its list yet');
		mod.jobsStore.upsert({ ...scope, status });
		assert.deepEqual(mod.stemSeparate.job(stableId), { id: scope.id, attempt: scope.attempt, status, error: null, scope: 'pending', confirmed: true });
		const outcome = await mod.stemSeparate.separate(stableId);
		assert.equal(outcome.ok, false);
		assert.match(outcome.reason, new RegExp(`library-wide .*scope pending.* already ${status} \\(job ${scope.id}\\)`));
		assert.deepEqual(posts, []);
	});
}

// Sol on #6819: a queued library-wide job takes only tracks whose audio is on
// disk and that have no bundle, which the client cannot see, so it is shown
// unconfirmed and a start asks the engine instead of refusing.
test('a queued library-wide scope job is unconfirmed: a start POSTs, and a 409 handing it back adopts it', async () => {
	await checked();
	const stableId = 'track-scope-unconfirmed';
	const scope = scopeRow({ status: 'queued' });
	mod.jobsStore.upsert(scope);
	assert.deepEqual(mod.stemSeparate.job(stableId), { id: scope.id, attempt: scope.attempt, status: 'queued', error: null, scope: 'pending', confirmed: false });
	postAnswer = 'duplicate';
	duplicateOf = scope;
	const outcome = await mod.stemSeparate.separate(stableId);
	assert.equal(posts.length, 1, 'the engine is asked');
	assert.equal(outcome.adopted, true);
	assert.equal(mod.stemSeparate.job(stableId)?.confirmed, true, 'once the engine named it, it is confirmed');
	const again = await mod.stemSeparate.separate(stableId);
	assert.equal(again.ok, false, 'a confirmed queued scope job refuses a second start');
	assert.equal(posts.length, 1);
});

test('a queued scope job the engine does not count for the track: the start enqueues the track\'s own job', async () => {
	await checked();
	const stableId = 'track-scope-skipped';
	mod.jobsStore.upsert(scopeRow({ status: 'queued' }));
	const outcome = await mod.stemSeparate.separate(stableId);
	assert.equal(outcome.ok, true);
	assert.equal(outcome.adopted, false);
	assert.equal(posts.length, 1);
	assert.equal(mod.stemSeparate.job(stableId)?.id, outcome.job_id);
	assert.equal(mod.stemSeparate.job(stableId)?.scope, 'track');
});

// Sol on #6819: a re-enqueue keeps the row's id and bumps attempt, so a scope
// run seen queued says nothing about the next attempt first seen running.
test('a re-enqueued scope run first seen running does not inherit the earlier attempt\'s queued sighting', async () => {
	await checked();
	const stableId = 'track-scope-reattempt';
	const scope = scopeRow({ status: 'queued' });
	mod.jobsStore.jobs = [scope];
	mod.stemSeparate.noteNoStems(stableId);
	assert.equal(mod.stemSeparate.job(stableId)?.id, scope.id, 'attempt seen queued covers the track');
	mod.jobsStore.jobs = [{ ...scope, status: 'running' }];
	assert.equal(mod.stemSeparate.job(stableId)?.status, 'running', 'control: the same run, now running, still covers it');
	mod.jobsStore.jobs = [{ ...scope, attempt: scope.attempt + 1, status: 'running' }];
	assert.equal(mod.stemSeparate.job(stableId), null, 'the next attempt was never seen queued for this track');
});

// Sol on #6819: a deck whose stems are ready is not in a scope worker's
// snapshot, so a lookup for it (the IPC snapshot asks for every loaded deck)
// must not record the queued scope run as covering it; only a deck showing NO
// STEMS does (noteNoStems).
test('a lookup for a ready deck records no scope coverage, so a running scope job does not refuse it later', async () => {
	await checked();
	const stableId = 'track-ready-then-gone';
	const scope = scopeRow({ status: 'queued' });
	mod.jobsStore.jobs = [scope];
	assert.equal(mod.stemSeparate.job(stableId)?.id, scope.id, 'a queued scope job reads as covering any track');
	mod.stemSeparate.snapshot(stableId);
	mod.jobsStore.jobs = [{ ...scope, status: 'running' }];
	assert.equal(mod.stemSeparate.job(stableId), null, 'the deck never showed NO STEMS while it was queued');
	const outcome = await mod.stemSeparate.separate(stableId);
	assert.equal(outcome.ok, true, 'its bundle vanished later: the separation is not refused');
	assert.equal(posts.length, 1);
	mod.jobsStore.jobs = [scope];
	mod.stemSeparate.noteNoStems('track-shown-none');
	mod.jobsStore.jobs = [{ ...scope, status: 'running' }];
	assert.equal(mod.stemSeparate.job('track-shown-none')?.status, 'running', 'control: a deck that showed NO STEMS keeps the coverage');
});

// Codex 4284606862: a scope job's worker takes its list of pending tracks ONCE,
// at startup (scripts/stems_r2_first_worker.py), so a scope job first seen
// already running may not include this track; the engine accepts the request
// (apps/engine_core/jobs/stems_dedup.py), and the deck must not refuse it.
for (const status of ['running', 'cancelling']) {
	test(`a scope job first seen ${status} neither blocks nor claims the track: separate() posts`, async () => {
		await checked();
		const stableId = `track-scope-started-${status}`;
		mod.jobsStore.jobs = [scopeRow({ status })];
		assert.equal(mod.stemSeparate.job(stableId), null);
		const outcome = await mod.stemSeparate.separate(stableId);
		assert.equal(outcome.ok, true, status);
		assert.equal(posts.length, 1);
	});
}

test('control: a scope job that finished before the track was looked at neither blocks nor claims it', async () => {
	await checked();
	for (const status of ['succeeded', 'failed', 'cancelled']) {
		mod.jobsStore.jobs = [scopeRow({ status })];
		assert.equal(mod.stemSeparate.job(`track-after-scope-${status}`), null, status);
	}
	const outcome = await mod.stemSeparate.separate('track-after-scope-succeeded');
	assert.equal(outcome.ok, true);
	assert.equal(posts.length, 1);
});

test('a scope job seen queued for the track still names it once it runs and finishes, so the deck reloads', () => {
	const scope = scopeRow({ status: 'queued' });
	mod.jobsStore.upsert(scope);
	mod.stemSeparate.noteNoStems('track-watched');
	assert.equal(mod.stemSeparate.job('track-watched').scope, 'pending');
	mod.jobsStore.upsert({ ...scope, status: 'running' });
	assert.equal(mod.stemSeparate.job('track-watched').status, 'running');
	mod.jobsStore.upsert({ ...scope, status: 'succeeded' });
	assert.deepEqual(mod.stemSeparate.job('track-watched'), { id: scope.id, attempt: scope.attempt, status: 'succeeded', error: null, scope: 'pending', confirmed: true });
});

test('control: with no job for the track, separate() enqueues the per-track job', async () => {
	await checked();
	const outcome = await mod.stemSeparate.separate('track-new');
	assert.equal(outcome.ok, true);
	assert.deepEqual(posts, [{ ...WIRE.enqueue_track.request, payload: { ...WIRE.enqueue_track.request.payload, stable_ids: ['track-new'] } }]);
	assert.equal(mod.stemSeparate.job('track-new').id, outcome.job_id);
});

test('control: a finished job for the track does not block separating again', async () => {
	await checked();
	mod.jobsStore.upsert(trackRow('track-retry', { status: 'failed', error: 'oom' }));
	const outcome = await mod.stemSeparate.separate('track-retry');
	assert.equal(outcome.ok, true);
	assert.equal(posts.length, 1);
});

test('stem_separate runs through the typed dispatcher and query() exposes its state', async () => {
	await checked();
	globalThis.window = {};
	const uninstall = mod.installPerformanceBrowserIpc();
	const deck = mod.deckStates[1];
	const saved = { stable_id: deck.stable_id, stems: deck.stems };
	try {
		const ipc = window.musicDjToolsPerformance;
		deck.stable_id = null;
		assert.equal(ipc.query().decks[1].stem_separation, null, 'no track, no separation state');
		await assert.rejects(ipc.dispatch({ type: 'stem_separate', deck: 1 }), /no track loaded/);
		await assert.rejects(ipc.dispatch({ type: 'stem_separate', deck: 1, extra: true }), /unexpected fields/i);
		assert.deepEqual(posts, []);
		// Both refusals came before the separation code loads; the query below
		// reads the store, so it is loaded here, as the deck's NO STEMS does.
		await mod.stemSeparation();

		deck.stable_id = 'track-ipc';
		deck.stems = { ...saved.stems, status: 'unavailable', error: 'no stem bundle for this track' };
		const before = ipc.query().decks[1].stem_separation;
		assert.equal(before.stable_id, 'track-ipc');
		assert.equal(before.job, null);
		assert.equal(before.starting, false);
		assert.equal(before.checking, false);

		// A click on a track the deck no longer holds is refused when the
		// command runs, before anything is enqueued (Codex on #6819).
		await assert.rejects(
			ipc.dispatch({ type: 'stem_separate', deck: 1, stable_id: 'track-before-the-load' }),
			/no longer holds track-before-the-load; nothing was separated/
		);
		assert.deepEqual(posts, []);

		// A command naming no track is bound to the track the deck holds when
		// it is received: a load landing before it runs is refused, never
		// separated (Codex on #6819, the CLI verb sends no stable_id).
		const raced = ipc.dispatch({ type: 'stem_separate', deck: 1 });
		deck.stable_id = 'track-after-the-load';
		await assert.rejects(raced, /no longer holds track-ipc; nothing was separated/);
		assert.deepEqual(posts, []);
		// An empty deck refuses on receipt: a load landing before the command
		// runs is never separated (Codex on #6819).
		deck.stable_id = null;
		const onEmpty = ipc.dispatch({ type: 'stem_separate', deck: 1 });
		deck.stable_id = 'track-loaded-after';
		await assert.rejects(onEmpty, /deck 1 has no track loaded/);
		assert.deepEqual(posts, []);
		deck.stable_id = 'track-ipc';

		// Control: naming the track the deck holds enqueues it.
		const state = await ipc.dispatch({ type: 'stem_separate', deck: 1, stable_id: 'track-ipc' });
		assert.equal(posts.length, 1);
		const after = state.decks[1].stem_separation;
		assert.equal(after.job.status, 'queued');
		assert.equal(after.job.scope, 'track');
		assert.equal(after.enqueue_error, null);
		assert.doesNotThrow(() => JSON.stringify(after), 'the query state is serializable');

		// A second command while that job is queued is refused, loudly, with no POST.
		await assert.rejects(ipc.dispatch({ type: 'stem_separate', deck: 1 }), /already queued/);
		assert.equal(posts.length, 1);
		assert.match(ipc.query().decks[1].command_error, /already queued/);

		// A library-wide job covering the track shows in the query, not only in job().
		// Seen queued for the track, then running: it has taken its list (Sol on #6819).
		const covering = scopeRow({ status: 'queued' });
		mod.jobsStore.jobs = [covering];
		mod.stemSeparate.noteNoStems('track-ipc-scope');
		mod.jobsStore.jobs = [{ ...covering, status: 'running' }];
		deck.stable_id = 'track-ipc-scope';
		assert.equal(ipc.query().decks[1].stem_separation.job.scope, 'pending');
		await assert.rejects(ipc.dispatch({ type: 'stem_separate', deck: 1 }), /library-wide/);
		assert.equal(posts.length, 1);
	} finally {
		deck.stable_id = saved.stable_id;
		deck.stems = saved.stems;
		uninstall();
		delete globalThis.window;
	}
});

// Codex 4301317322: the agent's stem_separate follows the button's load-first
// rule. An ended run may have written this track's bundle whatever its status,
// so the command is refused until a stem_load that RAN has looked for it, and
// the query says so (bundle_checked). The store's noteStemsLoaded is what the
// dispatcher's stem_load calls once retryStems returns; node cannot run the
// deck engine's audio side, so a stem_load here throws (no loaded deck), which
// is the control that a load that did not run is no look.
test('stem_separate after an ended job waits for a stem_load that ran, as the button does', async () => {
	await checked();
	globalThis.window = {};
	const uninstall = mod.installPerformanceBrowserIpc();
	const deck = mod.deckStates[1];
	const saved = { stable_id: deck.stable_id, stems: deck.stems };
	try {
		const ipc = window.musicDjToolsPerformance;
		deck.stable_id = 'track-load-first';
		deck.stems = mod.unavailableStemDeckState(NO_BUNDLE);
		const run = trackRow('track-load-first', { status: 'running' });
		mod.jobsStore.jobs = [run];
		assert.equal(ipc.query().decks[1].stem_separation.bundle_checked, null, 'control: a running job is not ended');
		mod.stemSeparate.noteStemsLoaded(1, 'track-load-first');
		mod.jobsStore.upsert({ ...run, status: 'failed', error: 'track-other: relay 503' });
		assert.equal(
			ipc.query().decks[1].stem_separation.bundle_checked,
			false,
			'a look while the job ran says nothing about what it wrote after'
		);
		await assert.rejects(ipc.dispatch({ type: 'stem_separate', deck: 1 }), /has not looked for the stems .* send stem_load for deck 1 first/);
		assert.equal(posts.length, 0, 'nothing was bought');

		await assert.rejects(ipc.dispatch({ type: 'stem_load', deck: 1 }));
		assert.equal(ipc.query().decks[1].stem_separation.bundle_checked, false, 'a stem_load that did not run is no look');
		await assert.rejects(ipc.dispatch({ type: 'stem_separate', deck: 1 }), /send stem_load/);
		assert.equal(posts.length, 0);

		mod.stemSeparate.noteStemsLoaded(1, 'track-load-first');
		assert.equal(ipc.query().decks[1].stem_separation.bundle_checked, true);
		const state = await ipc.dispatch({ type: 'stem_separate', deck: 1 });
		assert.equal(posts.length, 1, 'after the look found NO STEMS, a new run is enqueued');
		assert.equal(state.decks[1].stem_separation.job.status, 'queued');
		assert.equal(state.decks[1].stem_separation.bundle_checked, null, 'the new run is not ended');
	} finally {
		deck.stable_id = saved.stable_id;
		deck.stems = saved.stems;
		uninstall();
		delete globalThis.window;
	}
});

test('the dispatcher records a look only once stem_load\'s reload has settled, for the track it still holds', async () => {
	const { readFile } = await import('node:fs/promises');
	const source = await readFile(new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url), 'utf8');
	const start = source.indexOf("} else if (command.type === 'stem_load') {");
	assert.ok(start >= 0, 'the dispatcher has a stem_load branch');
	const body = source.slice(start, source.indexOf("} else if (command.type === 'stem_separate') {", start));
	const run = body.indexOf('const run = stemLoadRun(held);');
	const retry = body.indexOf('await engine.retryStems(command.deck);');
	const note = body.indexOf('void noteStemsLoadedOnceSettled(command.deck, held, run, () => getDeckState(command.deck));');
	assert.ok(run >= 0 && retry > run, 'the run the look answers for is read before the reload starts (Codex P1 on #6819)');
	assert.ok(retry >= 0 && note > retry, 'the look is recorded after the awaited retryStems, once settled');
	assert.equal(body.match(/noteStemsLoaded\w*\(/g)?.length, 1, 'no other path records the look');
});

// Sol P1 on #6819: retryStems returns once the reload has STARTED, so a look
// recorded then would let an agent's stem_separate buy a duplicate run while
// the existing bundle is still loading.
test('a dispatcher look is recorded only after the stems leave loading, and only for the held track', async () => {
	await checked();
	globalThis.window = {};
	const uninstall = mod.installPerformanceBrowserIpc();
	try {
		const ipc = window.musicDjToolsPerformance;
		const run = trackRow('track-settle-first', { status: 'running' });
		mod.jobsStore.jobs = [{ ...run, status: 'failed', finished_at: new Date().toISOString() }];
		const live = { stable_id: 'track-settle-first', stems: { status: 'loading' } };
		const look = mod.noteStemsLoadedOnceSettled(1, 'track-settle-first', mod.stemLoadRun('track-settle-first'), () => live, 5);
		await new Promise((resolve) => setTimeout(resolve, 30));
		const deck = mod.deckStates[1];
		const saved = { stable_id: deck.stable_id, stems: deck.stems };
		deck.stable_id = 'track-settle-first';
		deck.stems = mod.unavailableStemDeckState(NO_BUNDLE);
		try {
			assert.equal(ipc.query().decks[1].stem_separation.bundle_checked, false, 'no look while the stems are still loading');
			// A deck that moved on to another track records no look for the old one.
			const moved = { stable_id: 'track-other', stems: { status: 'ready' } };
			assert.equal(await mod.noteStemsLoadedOnceSettled(1, 'track-settle-first', mod.stemLoadRun('track-settle-first'), () => moved, 5), false);
			assert.equal(ipc.query().decks[1].stem_separation.bundle_checked, false, 'no look for a track the deck let go');
			live.stems.status = 'unavailable';
			assert.equal(await look, true);
			assert.equal(ipc.query().decks[1].stem_separation.bundle_checked, true, 'control: the look lands once the load settles');
		} finally {
			live.stems.status = 'unavailable'; // a failed assertion must not leave the look polling
			deck.stable_id = saved.stable_id;
			deck.stems = saved.stems;
		}
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

// Codex P1 on #6819: a stem_load that began while the covering job still ran
// can find no bundle, and if the job ends before the reload settles, that
// look must not count as one for the run that just ended: its bundle may
// have landed after the look. The look is credited only to the ended run it
// started under, and only while that run still covers the track.
test('a dispatcher look answers only for the ended run the load started under', async () => {
	await checked();
	globalThis.window = {};
	const uninstall = mod.installPerformanceBrowserIpc();
	const deck = mod.deckStates[1];
	const saved = { stable_id: deck.stable_id, stems: deck.stems };
	const live = { stable_id: 'track-bound-run', stems: { status: 'loading' } };
	try {
		const ipc = window.musicDjToolsPerformance;
		deck.stable_id = 'track-bound-run';
		deck.stems = mod.unavailableStemDeckState(NO_BUNDLE);
		const run = trackRow('track-bound-run', { status: 'running' });
		mod.jobsStore.jobs = [run];
		const startedUnder = mod.stemLoadRun('track-bound-run');
		assert.equal(startedUnder, null, 'a load starting while the job runs answers for no run');
		const look = mod.noteStemsLoadedOnceSettled(1, 'track-bound-run', startedUnder, () => live, 5);
		mod.jobsStore.upsert({ ...run, status: 'succeeded', finished_at: new Date().toISOString() });
		live.stems.status = 'unavailable';
		assert.equal(await look, true, 'the deck still holds the track');
		assert.equal(ipc.query().decks[1].stem_separation.bundle_checked, false, 'the look began before the run ended');

		// A look started under one ended attempt is not credited to the next.
		const first = mod.stemLoadRun('track-bound-run');
		assert.ok(first !== null, 'control: a load starting after the run ended answers for it');
		mod.jobsStore.upsert({ ...run, attempt: (run.attempt ?? 1) + 1, status: 'failed', error: 'relay 503' });
		assert.equal(await mod.noteStemsLoadedOnceSettled(1, 'track-bound-run', first, () => live, 5), true);
		assert.equal(ipc.query().decks[1].stem_separation.bundle_checked, false, 'a later attempt ended after the look began');

		// Control: a look started under the ended run that still covers the track counts.
		const now = mod.stemLoadRun('track-bound-run');
		assert.equal(await mod.noteStemsLoadedOnceSettled(1, 'track-bound-run', now, () => live, 5), true);
		assert.equal(ipc.query().decks[1].stem_separation.bundle_checked, true, 'control: the look lands for its own run');
	} finally {
		live.stems.status = 'unavailable'; // a failed assertion must not leave a look polling
		deck.stable_id = saved.stable_id;
		deck.stems = saved.stems;
		uninstall();
		delete globalThis.window;
	}
});

// Codex 4302614645: two decks can hold one track. A look on deck 1 says
// nothing about deck 2, whose own probe may have been skipped or failed, so
// deck 2's stem_separate still waits for a stem_load of its own rather than
// buying a run beside a bundle deck 1 is loading.
test('a stem_load on one deck is not a look for another deck holding the same track', async () => {
	await checked();
	globalThis.window = {};
	const uninstall = mod.installPerformanceBrowserIpc();
	const decks = [mod.deckStates[1], mod.deckStates[2]];
	const saved = decks.map((d) => ({ stable_id: d.stable_id, stems: d.stems }));
	try {
		const ipc = window.musicDjToolsPerformance;
		for (const d of decks) {
			d.stable_id = 'track-two-decks';
			d.stems = mod.unavailableStemDeckState(NO_BUNDLE);
		}
		mod.jobsStore.jobs = [trackRow('track-two-decks', { status: 'failed', error: 'relay 503' })];
		mod.stemSeparate.noteStemsLoaded(1, 'track-two-decks');

		assert.equal(ipc.query().decks[1].stem_separation.bundle_checked, true, 'control: deck 1 looked');
		assert.equal(ipc.query().decks[2].stem_separation.bundle_checked, false, 'deck 2 did not');
		await assert.rejects(ipc.dispatch({ type: 'stem_separate', deck: 2 }), /send stem_load for deck 2 first/);
		assert.equal(posts.length, 0, 'nothing was bought for deck 2');

		mod.stemSeparate.noteStemsLoaded(2, 'track-two-decks');
		await ipc.dispatch({ type: 'stem_separate', deck: 2 });
		assert.equal(posts.length, 1, 'control: once deck 2 looked, it may start a run');
	} finally {
		decks.forEach((d, i) => {
			d.stable_id = saved[i].stable_id;
			d.stems = saved[i].stems;
		});
		uninstall();
		delete globalThis.window;
	}
});

test('a 409 duplicate from the engine is adopted: the job is shown and remembered, and no error is recorded', async () => {
	await checked();
	postAnswer = 'duplicate';
	duplicateOf = trackRow('track-dup', { status: 'running' });
	assert.equal(mod.stemSeparate.job('track-dup'), null, 'precondition: this tab knows of no job');
	const outcome = await mod.stemSeparate.separate('track-dup');
	assert.deepEqual(outcome, { ok: true, job_id: duplicateOf.id, adopted: true });
	assert.equal(posts.length, 1);
	assert.deepEqual(mod.stemSeparate.job('track-dup'), { id: duplicateOf.id, attempt: duplicateOf.attempt, status: 'running', error: null, scope: 'track', confirmed: true });
	assert.equal(mod.stemSeparate.enqueueError('track-dup'), null);
	// It is remembered by id, so it still names the track once it finishes (the deck reloads).
	mod.jobsStore.upsert({ ...duplicateOf, status: 'succeeded' });
	assert.equal(mod.stemSeparate.job('track-dup').status, 'succeeded');
});

test('a 409 duplicate carrying a library-wide scope job is adopted as scope pending', async () => {
	await checked();
	postAnswer = 'duplicate';
	duplicateOf = scopeRow({ status: 'queued' });
	const outcome = await mod.stemSeparate.separate('track-dup-scope');
	assert.equal(outcome.adopted, true);
	mod.jobsStore.upsert({ ...duplicateOf, status: 'succeeded' });
	assert.deepEqual(mod.stemSeparate.job('track-dup-scope'), { id: duplicateOf.id, attempt: duplicateOf.attempt, status: 'succeeded', error: null, scope: 'pending', confirmed: true });
});

// Sol on #6819: the engine's atomic duplicate check now refuses a second
// scope request beside a queued scope job ((private path)),
// so the library prompt adopts that job instead of reporting a failure.
test('the library prompt adopts the queued scope job a duplicate 409 hands back', async () => {
	postAnswer = 'duplicate';
	duplicateOf = scopeRow({ status: 'queued' });
	const job = await mod.enqueueStemsForPending();
	assert.equal(job.id, duplicateOf.id);
	assert.equal(mod.jobsStore.jobs.find((row) => row.id === duplicateOf.id)?.status, 'queued', 'the adopted job is shown');
	// Control: a 409 that is not the duplicate refusal still fails the prompt.
	postAnswer = 'conflict-other';
	await assert.rejects(mod.enqueueStemsForPending(), /not a duplicate/);
});

test('control: a 409 that is not the duplicate refusal stays an error', async () => {
	await checked();
	postAnswer = 'conflict-other';
	duplicateOf = trackRow('track-other-409', { status: 'running' });
	const outcome = await mod.stemSeparate.separate('track-other-409');
	assert.equal(outcome.ok, false);
	assert.match(outcome.reason, /not a duplicate/);
	assert.equal(mod.stemSeparate.job('track-other-409'), null);
	assert.match(mod.stemSeparate.enqueueError('track-other-409'), /not a duplicate/);
});

test('stem_separate adopting a duplicate succeeds without a command error, and the query shows the job', async () => {
	await checked();
	globalThis.window = {};
	const uninstall = mod.installPerformanceBrowserIpc();
	const deck = mod.deckStates[2];
	const saved = { stable_id: deck.stable_id, stems: deck.stems };
	try {
		deck.stable_id = 'track-ipc-dup';
		deck.stems = { ...saved.stems, status: 'unavailable', error: 'no stem bundle for this track' };
		postAnswer = 'duplicate';
		duplicateOf = trackRow('track-ipc-dup', { status: 'running' });
		const state = await window.musicDjToolsPerformance.dispatch({ type: 'stem_separate', deck: 2 });
		assert.equal(state.decks[2].command_error, null);
		assert.deepEqual(state.decks[2].stem_separation.job, { id: duplicateOf.id, status: 'running', error: null, scope: 'track', confirmed: true });
	} finally {
		deck.stable_id = saved.stable_id;
		deck.stems = saved.stems;
		uninstall();
		delete globalThis.window;
	}
});

test('the deck SEPARATE button dispatches the typed command, not the store directly', async () => {
	const { readFile } = await import('node:fs/promises');
	const source = await readFile(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url), 'utf8');
	assert.match(source, /runPerformanceCommandFromUi\(\{ type: 'stem_separate', deck: deckId, stable_id: separateId \}\)/);
	assert.doesNotMatch(source, /(stemSeparate|separation)\.separate\(/);
});

// --- STEM-77: probe for the bundle however the covering job ended ---------

const ALIGNMENT = { sample_rate_hz: 44100, frame_count: 100, channel_count: 2, duration_ms: 2 };

/** One NO STEMS deck: the stems read model, the probe the deck wires, and how
 * the engine's stem probe answers (`bundle`). */
const NO_BUNDLE = 'no bundle on this machine and none indexed in the cloud';
function noStemsDeck(stableId, { bundle }) {
	const deck = { stems: mod.unavailableStemDeckState(NO_BUNDLE), reloads: 0, answers: 0 };
	assert.equal(mod.stemStatusView(deck.stems).name, 'none', 'the deck starts on NO STEMS');
	// As Deck.svelte's effect does while the deck shows NO STEMS.
	mod.stemSeparate.noteNoStems(stableId);
	const port = {
		landHeld: null,
		current: () => true,
		reload: () => {
			deck.reloads += 1;
			deck.stems = bundle
				? mod.readyStemDeckState({ source: 'demucs', model: 'htdemucs', layout: 'demucs4' }, ALIGNMENT)
				: mod.unavailableStemDeckState(NO_BUNDLE);
		}
	};
	// The stem_load command RAN (ok); `bundle` decides what it found.
	deck.probe = new mod.SeparateEndProbe(
		async () => {
			await mod.retryDeckStems(1, stableId, deck.stems, port);
			return true;
		},
		() => {
			deck.answers += 1;
		}
	);
	deck.view = () => {
		const job = mod.stemSeparate.job(stableId);
		return mod.separateActionView({
			refusal: null,
			checking: false,
			job,
			enqueueError: null,
			starting: false,
			probed: job !== null && deck.probe.probed(stableId, mod.separateRunId(job)),
			probeFailed: job !== null && deck.probe.failed(stableId, mod.separateRunId(job))
		});
	};
	return deck;
}

test('a failed library job that wrote this track loads its stems', async () => {
	const scope = scopeRow({ status: 'queued' });
	mod.jobsStore.upsert(scope);
	const deck = noStemsDeck('track-probe-hit', { bundle: true });
	assert.equal(deck.probe.observe('track-probe-hit', mod.stemSeparate.job('track-probe-hit')), null);
	mod.jobsStore.upsert({ ...scope, status: 'running' });
	assert.equal(deck.probe.observe('track-probe-hit', mod.stemSeparate.job('track-probe-hit')), null);
	assert.equal(deck.reloads, 0, 'a queued or running job is not probed yet');

	mod.jobsStore.upsert({ ...scope, status: 'failed', error: 'track-other: relay 503' });
	const job = mod.stemSeparate.job('track-probe-hit');
	assert.deepEqual([job.status, job.scope], ['failed', 'pending']);
	await deck.probe.observe('track-probe-hit', job);
	assert.equal(deck.reloads, 1);
	assert.equal(mod.stemStatusView(deck.stems).name, 'ready', 'the bundle the failed job wrote is on the deck');
});

test('control: a failed job with no bundle leaves NO STEMS and shows the failure only after the probe', async () => {
	mod.jobsStore.upsert(trackRow('track-probe-miss', { status: 'failed', error: 'relay 503' }));
	const deck = noStemsDeck('track-probe-miss', { bundle: false });
	const job = mod.stemSeparate.job('track-probe-miss');
	assert.equal(deck.view().label, 'CHECKING STEMS', 'no verdict before the deck has looked');
	assert.equal(deck.view().disabled, true);
	await deck.probe.observe('track-probe-miss', job);
	assert.equal(deck.reloads, 1);
	assert.equal(mod.stemStatusView(deck.stems).name, 'none');
	assert.equal(deck.answers, 1);
	const shown = deck.view();
	assert.equal(shown.label, 'SEPARATE AGAIN');
	assert.match(shown.tip, /relay 503/);
});

// Codex 4284606896: once the probe after a SUCCEEDED job found no bundle,
// another load finds nothing again, so the action is a new separation.
test('after a success whose probe found no bundle, the button separates again and a click posts', async () => {
	await checked();
	mod.jobsStore.jobs = [trackRow('track-probe-gone', { status: 'succeeded' })];
	const deck = noStemsDeck('track-probe-gone', { bundle: false });
	assert.equal(deck.view().label, 'CHECKING STEMS');
	await deck.probe.observe('track-probe-gone', mod.stemSeparate.job('track-probe-gone'));
	assert.equal(deck.reloads, 1, 'the probe ran stem_load once');
	assert.equal(mod.stemStatusView(deck.stems).name, 'none');
	const shown = deck.view();
	assert.deepEqual([shown.label, shown.disabled], ['SEPARATE AGAIN', false]);
	const outcome = await mod.stemSeparate.separate('track-probe-gone');
	assert.equal(outcome.ok, true);
	assert.equal(posts.length, 1, 'a replacement run was enqueued');
	assert.equal(deck.reloads, 1, 'and no second load probe');
});

test('the deck SEPARATE click always dispatches stem_separate, never another stem_load', async () => {
	const { readFile } = await import('node:fs/promises');
	const source = await readFile(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url), 'utf8');
	const start = source.indexOf('async function separateStems()');
	assert.ok(start >= 0, 'separateStems is the button handler');
	// Code only: the comment there may name stem_load to explain why it is not called.
	const body = source.slice(start, source.indexOf('\n\t}\n', start)).replace(/\/\/.*$/gm, '');
	assert.match(body, /runPerformanceCommandFromUi\(\{ type: 'stem_separate', deck: deckId, stable_id: separateId \}\)/);
	assert.doesNotMatch(body, /loadStems|stem_load/);
	assert.match(source, /onSeparate=\{separateStems\}/);
});

test('every ended status probes; queued, running and cancelling do not', async () => {
	for (const status of ['succeeded', 'failed', 'cancelled', 'unknown']) {
		const id = `track-probe-${status}`;
		mod.jobsStore.upsert(trackRow(id, { status }));
		const deck = noStemsDeck(id, { bundle: true });
		await deck.probe.observe(id, mod.stemSeparate.job(id));
		assert.equal(mod.stemStatusView(deck.stems).name, 'ready', status);
	}
	for (const status of ['queued', 'running', 'cancelling']) {
		const id = `track-probe-${status}`;
		mod.jobsStore.upsert(trackRow(id, { status }));
		const deck = noStemsDeck(id, { bundle: true });
		assert.equal(deck.probe.observe(id, mod.stemSeparate.job(id)), null, status);
		assert.equal(deck.reloads, 0, status);
	}
});

test('control (overshoot): an ended job is probed once per track, and another track it covered is probed too', async () => {
	const scope = scopeRow({ status: 'queued' });
	mod.jobsStore.upsert(scope);
	mod.stemSeparate.noteNoStems('track-probe-a');
	mod.stemSeparate.noteNoStems('track-probe-b');
	assert.equal(mod.stemSeparate.job('track-probe-a').scope, 'pending');
	assert.equal(mod.stemSeparate.job('track-probe-b').scope, 'pending');
	mod.jobsStore.upsert({ ...scope, status: 'failed', error: 'one track failed' });
	const deck = noStemsDeck('track-probe-a', { bundle: false });
	await deck.probe.observe('track-probe-a', mod.stemSeparate.job('track-probe-a'));
	assert.equal(deck.probe.observe('track-probe-a', mod.stemSeparate.job('track-probe-a')), null);
	assert.equal(deck.reloads, 1, 'the same ended job is not probed again for the same track');
	await deck.probe.observe('track-probe-b', mod.stemSeparate.job('track-probe-b'));
	assert.equal(deck.reloads, 2, 'the deck loaded another track the job covered');
});

test('the deck wires the probe to every ended covering job and passes its answer to the button', async () => {
	const { readFile } = await import('node:fs/promises');
	const source = await readFile(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url), 'utf8');
	assert.match(
		source,
		/new SeparateEndProbe\(\s*async \(stableId\) =>\s*separateId === stableId &&\s*lookForBundle\(\{\s*run: async \(\) => \(await runPerformanceCommandFromUi\(\{ type: 'stem_load', deck: deckId, stable_id: stableId \}\)\)\.ok,\s*settled: stemsSettled,\s*holds: \(\) => deck\.stable_id === stableId\s*\}\),/,
		'the probe waits for the stem load it started and answers only for the track the deck still holds'
	);
	assert.match(source, /if \(deck\.stems\.status !== 'loading'\) return Promise\.resolve\(\);/);
	assert.match(source, /separateProbeFailed\(separation, separateId\)/);
	assert.match(source, /separation\.noteNoStems\(separateId\);\s*void separateProbe\.observe\(separateId, separation\.job\(separateId\)\)/);
	assert.doesNotMatch(source, /status === 'succeeded' && reloadedForJob/);
});

// Codex 4301903359 (#6819): stem_load returns once the deck STARTED its
// stem reload, so the look must wait for that load to settle before it
// answers, or SEPARATE AGAIN is offered while the bundle is still loading.
test('a look waits for the stem load it started to settle before it answers', async () => {
	let settle;
	const settled = new Promise((resolve) => (settle = resolve));
	let holding = true;
	let answered = null;
	const look = mod.lookForBundle({ run: async () => true, settled: () => settled, holds: () => holding }).then((v) => (answered = v));
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(answered, null, 'no answer while the load is still running');
	settle();
	await look;
	assert.equal(answered, true, 'the settled load answers for the held track');
	holding = false;
	assert.equal(
		await mod.lookForBundle({ run: async () => true, settled: async () => {}, holds: () => holding }),
		false,
		'a deck that moved on while the load ran is no answer'
	);
	let waited = false;
	assert.equal(
		await mod.lookForBundle({ run: async () => false, settled: async () => { waited = true; }, holds: () => true }),
		false,
		'control: a stem_load that did not run is no answer, and nothing waits'
	);
	assert.equal(waited, false);
});

// Sol 4291048002: the probe is told which track it answers for, so a deck
// that moved on can decline to answer for the old one.
test('the probe is given its track, and a probe declined for a track it left is no answer', async () => {
	const asked = [];
	const holding = 'track-now';
	const probe = new mod.SeparateEndProbe(async (stableId) => {
		asked.push(stableId);
		return stableId === holding;
	});
	await probe.observe('track-left', { id: 'job-left', status: 'succeeded' });
	await probe.observe(holding, { id: 'job-now', status: 'succeeded' });
	assert.deepEqual(asked, ['track-left', holding]);
	assert.equal(probe.probed('track-left', 'job-left'), false, 'no answer for a track the deck left');
	assert.equal(probe.failed('track-left', 'job-left'), true);
	assert.equal(probe.probed(holding, 'job-now'), true, 'control: the held track is answered');
});

// Codex 4291171637: a re-enqueue keeps the row's id and bumps attempt, so the
// rerun's end must be probed again, not answered from the first run.
test('a re-enqueued run of the same job is probed again when it ends', async () => {
	let runs = 0;
	const probe = new mod.SeparateEndProbe(async () => {
		runs += 1;
		return true;
	});
	const first = { id: 'job-rerun', attempt: 1, status: 'failed' };
	await probe.observe('track-rerun', first);
	assert.equal(probe.probed('track-rerun', mod.separateRunId(first)), true);

	const rerun = { id: 'job-rerun', attempt: 2, status: 'succeeded' };
	assert.equal(probe.probed('track-rerun', mod.separateRunId(rerun)), false, 'the first run answers nothing about the rerun');
	assert.notEqual(probe.observe('track-rerun', rerun), null, 'the rerun is probed');
	await Promise.resolve();
	assert.equal(runs, 2);
	assert.equal(probe.observe('track-rerun', rerun), null, 'control: one run is still probed once');
	assert.equal(mod.separateRunId({ id: 'j' }), 'j', 'a job with no attempt keys on its id');
});

// Codex 4285208896: a deck that moves from A to B with both probes pending.
function pendingProbe() {
	const pending = [];
	const probe = new mod.SeparateEndProbe(
		() =>
			new Promise((resolve) => {
				pending.push(() => resolve(true));
			})
	);
	return { probe, pending };
}

test('a late answer for the track the deck left does not leave the new track on CHECKING', async () => {
	const { probe, pending } = pendingProbe();
	const a = probe.observe('track-a', { id: 'job-a', status: 'failed' });
	const b = probe.observe('track-b', { id: 'job-b', status: 'failed' });
	assert.equal(pending.length, 2, 'both probes started');
	pending[1]();
	await b;
	pending[0]();
	await a;
	assert.equal(probe.probed('track-b', 'job-b'), true, 'B answered first and stays answered');
	assert.equal(probe.probed('track-a', 'job-a'), true);
	assert.equal(probe.observe('track-b', { id: 'job-b', status: 'failed' }), null, 'and is not probed again');
	assert.equal(pending.length, 2);
});

test('control: in order, each answer lands; the same key never restarts; another job for the track does', async () => {
	const { probe, pending } = pendingProbe();
	const a = probe.observe('track-c', { id: 'job-c', status: 'failed' });
	assert.equal(probe.observe('track-c', { id: 'job-c', status: 'failed' }), null, 'a pending key is not restarted');
	assert.equal(probe.probed('track-c', 'job-c'), false, 'not answered before it answers');
	pending[0]();
	await a;
	assert.equal(probe.probed('track-c', 'job-c'), true);
	const d = probe.observe('track-d', { id: 'job-d', status: 'succeeded' });
	pending[1]();
	await d;
	assert.equal(probe.probed('track-d', 'job-d'), true);
	assert.equal(probe.observe('track-c', { id: 'job-c', status: 'failed' }), null, 'an answered key is not restarted either');
	assert.notEqual(probe.observe('track-c', { id: 'job-c2', status: 'failed' }), null, 'a new ended job for the track is probed');
	assert.equal(pending.length, 3);
});

// Codex 4286181044: a stem_load that did not RUN (refused by a preset lock:
// runPerformanceCommandFromUi resolves {ok: false}; or thrown) is no answer
// about the bundle, so it must not enable SEPARATE AGAIN.
for (const [how, probeFn] of [
	['refused', async () => false],
	[
		'thrown',
		async () => {
			throw new Error('deck 1 has no decoded mix');
		}
	]
]) {
	test(`a ${how} stem_load leaves the probe unanswered: CHECK STEMS, no loop, and a retry that runs answers`, async () => {
		const id = `track-probe-${how}`;
		mod.jobsStore.upsert({ ...trackRow(id, { status: 'succeeded' }), finished_at: '2026-10-15T12:00:00.000+00:00' });
		const job = mod.stemSeparate.job(id);
		let runs = 0;
		let ran = false;
		const probe = new mod.SeparateEndProbe(async () => {
			runs += 1;
			return ran ? true : probeFn();
		});
		const view = () =>
			mod.separateActionView({
				refusal: null,
				checking: false,
				job,
				enqueueError: null,
				starting: false,
				probed: probe.probed(id, mod.separateRunId(job)),
				probeFailed: probe.failed(id, mod.separateRunId(job))
			});
		if (how === 'thrown') {
			// A throw is a fault, not a refusal: kept and rethrown (Sol on #6819).
			await assert.rejects(probe.observe(id, job), /no decoded mix/);
			assert.match(String(probe.error(id, mod.separateRunId(job))), /no decoded mix/);
		} else {
			await probe.observe(id, job);
			assert.equal(probe.error(id, mod.separateRunId(job)), undefined, 'control: a refusal is no error');
		}
		assert.equal(probe.probed(id, mod.separateRunId(job)), false, 'nothing is known about the bundle');
		assert.deepEqual([view().label, view().disabled], ['CHECK STEMS', false], 'never SEPARATE AGAIN');
		assert.equal(probe.observe(id, job), null, 'not retried on its own (each try toasts)');
		assert.equal(runs, 1);
		ran = true;
		await probe.retry(id, job);
		assert.equal(runs, 2);
		assert.equal(probe.probed(id, mod.separateRunId(job)), true);
		assert.equal(probe.failed(id, mod.separateRunId(job)), false);
		assert.equal(probe.error(id, mod.separateRunId(job)), undefined, 'a retry that ran clears the kept error');
		assert.deepEqual([view().label, view().disabled], ['SEPARATE AGAIN', false]);
		assert.equal(probe.retry(id, job), null, 'an answered probe is not retried');
	});
}

test('control: a stem_load that ran and found no bundle answers, so SEPARATE AGAIN is offered', async () => {
	const id = 'track-probe-ran-empty';
	mod.jobsStore.upsert(trackRow(id, { status: 'succeeded' }));
	const deck = noStemsDeck(id, { bundle: false });
	await deck.probe.observe(id, mod.stemSeparate.job(id));
	assert.equal(deck.reloads, 1);
	assert.equal(deck.probe.failed(id, mod.separateRunId(mod.stemSeparate.job(id))), false);
	assert.equal(deck.view().label, 'SEPARATE AGAIN');
});

test('the deck CHECK STEMS click looks again rather than separating', async () => {
	const { readFile } = await import('node:fs/promises');
	const source = await readFile(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url), 'utf8');
	const start = source.indexOf('async function separateStems()');
	const body = source.slice(start, source.indexOf('\n\t}\n', start)).replace(/\/\/.*$/gm, '');
	const retryAt = body.indexOf('separateProbe.retry(separateId, job)');
	assert.ok(retryAt >= 0, 'a failed probe is retried');
	assert.ok(body.indexOf('separateProbe.failed(separateId, separateRunId(job))') < retryAt);
	assert.match(body.slice(retryAt), /^separateProbe\.retry\(separateId, job\);\s*return;/);
});

// --- STEM-77: the separation store stays out of the first load ------------
//
// CI's bundle budget (scripts/check-bundle-size.sh) went over on #6819 when
// performance-ipc.svelte.ts (initial load of "/") and Deck.svelte (first load
// of /performance) imported the store statically: it drags in the jobs store
// and the stems job client. Both reach it through stem-separation-port.ts.

test('neither the dispatcher nor the deck imports the separation store statically', async () => {
	const { readFile } = await import('node:fs/promises');
	for (const file of ['src/lib/rb/performance-ipc.svelte.ts', 'src/lib/components/rb/Deck.svelte']) {
		const source = await readFile(new URL(`../../${file}`, import.meta.url), 'utf8');
		const statics = [...source.matchAll(/^\s*import\s+(?!type\b)[^;]*?from\s+'([^']+)'/gm)].map((m) => m[1]);
		assert.ok(statics.length > 5, `${file}: the import scan found its imports`);
		assert.ok(statics.includes('$lib/rb/stem-separation-port'), `${file} reaches the store through the port`);
		assert.ok(!statics.includes('$lib/rb/stem-separate.svelte'), `${file} must not import the store statically`);
		assert.ok(!statics.includes('$lib/rb/stem-separate-view'), `${file}: the button's words load with the store`);
	}
	// stem-status.ts is in the first load of "/" (the port reads stemStatusView).
	const status = await readFile(new URL('../../src/lib/rb/stem-status.ts', import.meta.url), 'utf8');
	assert.doesNotMatch(status, /separateActionView|stem-separate-view/);
});

test('the port hands back the one store, loaded on demand and kept', async () => {
	assert.equal(await mod.stemSeparation(), mod.stemSeparate);
	assert.equal(mod.loadedStemSeparation(), mod.stemSeparate, 'the load is kept for the next query');
	assert.equal(await mod.stemSeparation(), mod.stemSeparate);
});

// The quality gate's frontend.import_cycles (scripts/quality_gate.py) went up
// on #6819: the store imported the port to register itself while the port
// imports the store. The port now keeps the store it loads instead.
test('the separation store does not import its port, so the two form no cycle', async () => {
	const { readFile } = await import('node:fs/promises');
	const store = await readFile(new URL('../../src/lib/rb/stem-separate.svelte.ts', import.meta.url), 'utf8');
	const port = await readFile(new URL('../../src/lib/rb/stem-separation-port.ts', import.meta.url), 'utf8');
	assert.match(port, /import\('\.\/stem-separate\.svelte'\)/, 'control: the port does load the store');
	assert.doesNotMatch(store, /(?:from\s+|import\(\s*)'[^']*stem-separation-port'/);
});

// Codex 4285208885: a store chunk that fails to load is a refusal the query
// reports in the deck's own words, not a silent null.
test('a failed load of the separation store shows in stem_separation as the refusal; an unloaded one is null', async () => {
	const lazy = await loadTypeScriptModule('tests/unit/fixtures/stem-separation-unloaded-entry.ts', {
		viteApiBase: API_BASE,
		failImports: [/stem-separate\.svelte$/]
	});
	globalThis.window = {};
	const uninstall = lazy.installPerformanceBrowserIpc();
	try {
		const ipc = window.musicDjToolsPerformance;
		const deck = lazy.deckStates[1];
		deck.stable_id = 'track-lazy';
		deck.stems = lazy.unavailableStemDeckState('no stem bundle for this track');
		assert.equal(lazy.loadedStemSeparation(), null, 'precondition: nothing loaded the store');
		assert.equal(ipc.query().decks[1].stem_separation, null, 'control: not loaded and not failed is null');
		assert.equal(lazy.loadedStemSeparation(), null, 'and the query did not load it');

		let thrown = null;
		await assert.rejects(ipc.dispatch({ type: 'stem_separate', deck: 1 }), (exc) => {
			thrown = exc;
			return true;
		});
		const reason = lazy.stemSeparationLoadError();
		assert.ok(thrown.message.includes(reason), 'the command fails with the recorded text, which the deck shows too');
		assert.match(reason, /^the stem separation code could not be loaded \(.*Failed to fetch dynamically imported module/);
		assert.deepEqual(ipc.query().decks[1].stem_separation, {
			stable_id: 'track-lazy',
			job: null,
			starting: false,
			enqueue_error: null,
			refusal: reason,
			checking: false,
			bundle_checked: null
		});
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

// Codex 4349079287: the first stem_separate loads the store's chunk, and a
// load of another track can land during that fetch, so the deck is read
// again only once the store is there.
test('stem_separate reads the deck only after the store has loaded, and refuses a track the deck no longer holds', async () => {
	const lazy = await loadTypeScriptModule('tests/unit/fixtures/stem-separation-unloaded-entry.ts', {
		viteApiBase: API_BASE
	});
	assert.equal(lazy.loadedStemSeparation(), null, 'precondition: nothing loaded the store');
	const storeAtRead = [];
	const read = (deck) => () => {
		storeAtRead.push(lazy.loadedStemSeparation());
		return deck;
	};
	// The load of track-b landed while the store was loading.
	await assert.rejects(
		lazy.separateLoadedDeck(1, 'track-a', read({ stable_id: 'track-b', stems: lazy.unavailableStemDeckState('no bundle') })),
		/deck 1 no longer holds track-a; nothing was separated/
	);
	assert.equal(storeAtRead.length, 1);
	assert.notEqual(storeAtRead[0], null, 'the deck was read after the store loaded, not before');
	// Control: the deck still holds the track, so the store judges what the
	// deck shows NOW (stems probing), not what it showed when issued.
	await assert.rejects(
		lazy.separateLoadedDeck(1, 'track-a', read({ stable_id: 'track-a', stems: lazy.loadingStemDeckState() })),
		/deck 1 does not show NO STEMS \(stems are probing\)/
	);
	assert.deepEqual(posts, [], 'nothing was enqueued');
});

test('Rust engine mode refuses stem_separate by name and the query reports it, never starting a job', async () => {
	// [if] /performance runs the Rust engine [then] an agent's stem_separate
	// fails as RUST_ENGINE_UNAVAILABLE, the store never loads (so no job can
	// be enqueued), and stem_separation carries the refusal the inert button
	// shows (Codex on #6819). Control: with the mode off the query is null.
	const rust = await loadTypeScriptModule('tests/unit/fixtures/stem-separation-rust-entry.ts', {
		viteApiBase: API_BASE
	});
	globalThis.window = {};
	const uninstall = rust.installPerformanceBrowserIpc();
	try {
		const ipc = window.musicDjToolsPerformance;
		const deck = rust.deckStates[1];
		deck.stable_id = 'track-rust';
		deck.stems = rust.unavailableStemDeckState('no stem bundle for this track');
		assert.equal(ipc.query().decks[1].stem_separation, null, 'control: Web Audio mode, nothing loaded');
		rust.rustMode.enabled = true;
		try {
			await assert.rejects(
				ipc.dispatch({ type: 'stem_separate', deck: 1 }),
				/RUST_ENGINE_UNAVAILABLE: stem_separate .* see PARITY-TODO/
			);
			assert.equal(rust.loadedStemSeparation(), null, 'the separation store never loaded');
			assert.deepEqual(ipc.query().decks[1].stem_separation, {
				stable_id: 'track-rust',
				job: null,
				starting: false,
				enqueue_error: null,
				refusal: rust.RUST_SEPARATION_REFUSAL,
				checking: false,
				bundle_checked: null
			});
		} finally {
			rust.rustMode.enabled = false;
		}
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('before the store loads, the deck says what separateActionView would, in the same words', async () => {
	const { readFile } = await import('node:fs/promises');
	const source = await readFile(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url), 'utf8');
	const base = { refusal: null, checking: false, job: null, enqueueError: null, starting: false, probed: false };
	const checking = mod.separateActionView({ ...base, checking: true });
	assert.ok(source.includes(`'${checking.tip}'`), `the deck's checking tip is "${checking.tip}"`);
	const refused = mod.separateActionView({ ...base, refusal: '@@' }).tip.replace('@@', '${separationLoadError}');
	assert.ok(source.includes(`\`${refused}\``), `the deck's refusal tip is "${refused}"`);
	assert.equal(checking.label, 'SEPARATE');
	assert.match(source, /label: 'SEPARATE',\s+tip:\s+separationLoadError === null[\s\S]{0,300}?disabled: true/);
	assert.match(source, /separation\.actionView\(\s*separateId,\s*separateProbed\(separation, separateId\),\s*separateProbeFailed\(separation, separateId\)\s*\)/);
});

test('the deck shows the port\'s load failure text, not its own', async () => {
	const { readFile } = await import('node:fs/promises');
	const source = await readFile(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url), 'utf8');
	assert.match(source, /separationLoadError = stemSeparationLoadError\(\)/);
	assert.doesNotMatch(source, /could not be loaded/);
});

// --- STEM-77: only this engine's library covers the deck ------------------

test('the engine library is the data dir its health names', () => {
	assert.equal(mod.engineLibraryDir(), ENGINE_DATA_DIR);
	assert.equal(mod.engineLibraryDir('C:\\Users\\dev\\data\\state\\state.db'), 'C:/Users/dev/data');
	assert.equal(mod.engineLibraryDir(null), null);
});

test('a scope job for another library neither blocks nor claims the deck', async () => {
	await checked();
	mod.jobsStore.upsert(scopeRow({ status: 'queued' }));
	const other = scopeRow({ status: 'queued' });
	other.payload = { ...other.payload, data_dir: '/other/library' };
	mod.jobsStore.jobs = [other];
	assert.equal(mod.stemSeparate.job('track-other-lib'), null);
	const outcome = await mod.stemSeparate.separate('track-other-lib');
	assert.equal(outcome.ok, true);
	assert.equal(posts.length, 1);
});

test('a per-track job for another library does not claim the deck either', () => {
	const other = trackRow('track-other-lib-ids', { status: 'running' });
	other.payload = { ...other.payload, data_dir: '/other/library' };
	mod.jobsStore.upsert(other);
	assert.equal(mod.stemSeparate.job('track-other-lib-ids'), null);
});

test('control: a job naming this library, in any spelling, or none still blocks', async () => {
	await checked();
	for (const dataDir of [undefined, ENGINE_DATA_DIR, `${ENGINE_DATA_DIR}/`, `${ENGINE_DATA_DIR}/state/..`]) {
		const row = scopeRow({ status: 'queued' });
		if (dataDir !== undefined) row.payload = { ...row.payload, data_dir: dataDir };
		mod.jobsStore.jobs = [row];
		const id = `track-same-lib-${String(dataDir)}`;
		assert.equal(mod.stemSeparate.job(id)?.id, row.id, String(dataDir));
		// Running once seen queued, so it is confirmed and refuses (Sol on #6819).
		mod.stemSeparate.noteNoStems(id);
		mod.jobsStore.jobs = [{ ...row, status: 'running' }];
		const outcome = await mod.stemSeparate.separate(id);
		assert.equal(outcome.ok, false, String(dataDir));
	}
	const own = trackRow('track-same-lib-ids', { status: 'running' });
	own.payload = { ...own.payload, data_dir: ENGINE_DATA_DIR };
	mod.jobsStore.jobs = [own];
	assert.equal(mod.stemSeparate.job('track-same-lib-ids')?.id, own.id);
	assert.equal(posts.length, 0);
});

test('a remembered job the refreshed list no longer holds is not shown as queued (Sol on #6819)', async () => {
	await checked();
	const started = await mod.stemSeparate.separate('track-dropped');
	assert.equal(started.ok, true);
	assert.equal(mod.stemSeparate.job('track-dropped')?.id, started.job_id, 'control: the started job is followed');
	// A refetch of the newest JOBS_LIST_LIMIT rows no longer includes it.
	mod.jobsStore.jobs = [];
	assert.equal(mod.stemSeparate.job('track-dropped'), null, 'no row says the job is still active');
	const view = mod.stemSeparate.actionView('track-dropped', false);
	assert.equal(view.disabled, false, `a retry is offered, got ${view.label}`);
});

test('an unconfirmed queued library job offers SEPARATE; a confirmed one, or one behind a refusal, does not (Sol on #6819)', () => {
	const base = { refusal: null, checking: false, enqueueError: null, starting: false, probed: false };
	const job = { id: 'job-scope', status: 'queued', error: null, scope: 'pending' };
	const open = mod.separateActionView({ ...base, job: { ...job, confirmed: false } });
	assert.equal(open.label, 'SEPARATE');
	assert.equal(open.disabled, false);
	assert.match(open.tip, /may include this track/);
	// Controls: a confirmed job keeps it shut as QUEUED; a refusal or a check
	// in flight keeps it shut too, and says that, never QUEUED.
	assert.equal(mod.separateActionView({ ...base, job: { ...job, confirmed: true } }).label, 'QUEUED');
	const refused = mod.separateActionView({ ...base, refusal: 'no relay', job: { ...job, confirmed: false } });
	assert.deepEqual([refused.label, refused.disabled], ['SEPARATE', true]);
	assert.match(refused.tip, /not available here: no relay/);
	const checking = mod.separateActionView({ ...base, checking: true, job: { ...job, confirmed: false } });
	assert.deepEqual([checking.label, checking.disabled], ['SEPARATE', true]);
	assert.match(checking.tip, /Checking whether this machine/);
});

test('an unconfirmed job at another tier offers to ask the engine, queued or running, never QUEUED (Sol on #6819)', () => {
	const base = { refusal: null, checking: false, enqueueError: null, starting: false, probed: false };
	for (const status of ['queued', 'running', 'cancelling']) {
		const view = mod.separateActionView({ ...base, job: { id: 'job-l', status, error: null, scope: 'track', confirmed: false } });
		assert.deepEqual([view.label, view.disabled], ['SEPARATE', false], status);
		assert.match(view.tip, /another tier or on another executor/);
	}
});

test('a job at another tier or executor is shown but does not refuse a start (Sol on #6819)', async () => {
	await checked();
	for (const [stableId, change] of [['track-tier-l', { tier: 'L' }], ['track-exec-local', { executor: 'local', tier: 'LOCAL' }]]) {
		const row = trackRow(stableId, { status: 'running' });
		mod.jobsStore.upsert({ ...row, payload: { ...row.payload, ...change } });
		assert.equal(mod.stemSeparate.job(stableId)?.confirmed, false, stableId);
		const started = await mod.stemSeparate.separate(stableId);
		assert.equal(started.ok, true, stableId);
		assert.equal(started.adopted, false, 'the engine counts no row of another config, so it starts one');
	}
	// Control: a row at the deck's own tier and executor still refuses, with no POST.
	const sent = posts.length;
	mod.jobsStore.upsert(trackRow('track-same-config', { status: 'running' }));
	assert.equal(mod.stemSeparate.job('track-same-config')?.confirmed, true);
	assert.equal((await mod.stemSeparate.separate('track-same-config')).ok, false);
	assert.equal(posts.length, sent);
});

// Sol P1 on #6819: a spawned row runs what its recorded argv says, as the
// engine judges it (stems_dedup._runs_as), not what its payload asked for.
test('a running row is judged by the worker argv it launched with, not its payload', async () => {
	await checked();
	const local = trackRow('track-launched-local', { status: 'running' });
	const { tier, executor } = local.payload;
	// The payload names the deck's config, but policy sent the worker local.
	mod.jobsStore.upsert({ ...local, worker_argv: ['/usr/bin/python3', '/app/scripts/stems_local_worker.py', '--ids', 'track-launched-local'] });
	assert.equal(mod.stemSeparate.job('track-launched-local')?.confirmed, false, 'a local launch does not make the deck\'s stems');
	const sent = posts.length;
	const started = await mod.stemSeparate.separate('track-launched-local');
	assert.equal(started.ok, true, 'the start asks the engine, which counts no row at this config');
	assert.equal(posts.length, sent + 1);
	// Control (overshoot): a payload at another tier whose argv launched at the
	// deck's own config is the engine's duplicate, so it refuses with no POST.
	const launched = trackRow('track-launched-mine', { status: 'running' });
	mod.jobsStore.upsert({
		...launched,
		payload: { ...launched.payload, tier: 'L' },
		worker_argv: ['python', 'scripts/stems_r2_first_worker.py', '--executor', executor, '--tier', tier]
	});
	assert.equal(mod.stemSeparate.job('track-launched-mine')?.confirmed, true);
	assert.equal((await mod.stemSeparate.separate('track-launched-mine')).ok, false);
	assert.equal(posts.length, sent + 1, 'no second POST');
});

test('launchedConfig reads the three worker argv shapes the engine reads, and nothing else', () => {
	assert.deepEqual(mod.launchedConfig(['py', 'scripts/stems_r2_first_worker.py', '--executor', 'modal', '--tier', 'M']), { tier: 'M', executor: 'modal' });
	assert.deepEqual(mod.launchedConfig(['py', 'C:\\app\\scripts\\stems_local_worker.py']), { tier: 'LOCAL', executor: 'local' });
	assert.deepEqual(mod.launchedConfig(['py', 'scripts/stems_modal_worker.py', '--tier', 'L']), { tier: 'L', executor: 'modal' });
	assert.equal(mod.launchedConfig(['py', 'scripts/stems_r2_first_worker.py', '--tier', 'M']), null, 'no executor flag');
	assert.equal(mod.launchedConfig(['py', 'scripts/other.py', '--tier', 'M']), null);
	assert.equal(mod.launchedConfig([]), null);
});

test('a newer run at another tier never hides an older one at the deck\'s own config, running or ended (Codex on #6819)', async () => {
	await checked();
	const stableId = 'track-m-under-l';
	const mine = trackRow(stableId, { status: 'running' });
	mod.jobsStore.upsert(mine);
	const other = trackRow(stableId, { status: 'running' });
	mod.jobsStore.upsert({ ...other, payload: { ...other.payload, tier: 'L' } });
	assert.equal(mod.jobsStore.jobs.findIndex((row) => row.id === other.id) < mod.jobsStore.jobs.findIndex((row) => row.id === mine.id), true, 'the other-tier run lists first');
	assert.deepEqual([mod.stemSeparate.job(stableId)?.id, mod.stemSeparate.job(stableId)?.confirmed], [mine.id, true]);
	// Once the deck's own run ends, the deck follows it, so it probes its bundle.
	mod.jobsStore.upsert({ ...mine, status: 'succeeded', finished_at: new Date().toISOString() });
	assert.deepEqual([mod.stemSeparate.job(stableId)?.id, mod.stemSeparate.job(stableId)?.status], [mine.id, 'succeeded']);
	// Control: with no run at the deck's config at all, the other-tier run is still shown, unconfirmed.
	const lone = 'track-l-only';
	const l = trackRow(lone, { status: 'running' });
	mod.jobsStore.upsert({ ...l, payload: { ...l.payload, tier: 'L' } });
	assert.deepEqual([mod.stemSeparate.job(lone)?.id, mod.stemSeparate.job(lone)?.confirmed], [l.id, false]);
	// The job this deck started beats a newer active run at its config: it is the engine's own answer.
	const ownTrack = 'track-own-under-newer';
	const started = await mod.stemSeparate.separate(ownTrack);
	assert.equal(started.adopted, false);
	const newer = trackRow(ownTrack, { status: 'running' });
	mod.jobsStore.upsert(newer);
	assert.equal(mod.jobsStore.jobs[0].id, newer.id, 'the other run lists first');
	assert.equal(mod.stemSeparate.job(ownTrack)?.id, started.job_id);
});

test('a 409 handing back a per-track job forgets the library job seen beside the track; an adopted scope job still covers it (Sol on #6819)', async () => {
	await checked();
	const scope = scopeRow({ status: 'queued' });
	mod.jobsStore.upsert(scope);
	mod.stemSeparate.noteNoStems('track-adopts-own');
	mod.stemSeparate.noteNoStems('track-adopts-scope');
	postAnswer = 'duplicate';
	const own = trackRow('track-adopts-own', { status: 'running' });
	duplicateOf = own;
	assert.equal((await mod.stemSeparate.separate('track-adopts-own')).adopted, true);
	duplicateOf = scope;
	assert.equal((await mod.stemSeparate.separate('track-adopts-scope')).adopted, true);
	// The adopted per-track job ends; the library job runs on without that track.
	mod.jobsStore.upsert({ ...own, status: 'succeeded' });
	mod.jobsStore.upsert({ ...scope, status: 'running' });
	assert.equal(mod.stemSeparate.job('track-adopts-own')?.id, own.id, 'the track follows the job it adopted');
	// Control: the track that adopted the library job still follows it as it runs.
	assert.equal(mod.stemSeparate.job('track-adopts-scope')?.id, scope.id);
});

test('a track the engine took a job for forgets the queued library job it was seen beside (Sol on #6819)', async () => {
	await checked();
	const stableId = 'track-own-after-scope';
	const scope = scopeRow({ status: 'queued' });
	mod.jobsStore.upsert(scope);
	mod.stemSeparate.noteNoStems(stableId);
	mod.stemSeparate.noteNoStems('track-beside-scope');
	const started = await mod.stemSeparate.separate(stableId);
	assert.equal(started.adopted, false, 'the engine gave the track a job of its own');
	const own = mod.jobsStore.jobs.find((row) => row.id === started.job_id);
	mod.jobsStore.upsert({ ...own, status: 'succeeded' });
	// The library job runs without this track; it must not refuse a retry.
	mod.jobsStore.upsert({ ...scope, status: 'running' });
	assert.equal(mod.stemSeparate.job(stableId)?.id, started.job_id, 'the track follows its own job');
	// Control: a track seen beside it that asked nothing still follows the library job.
	assert.equal(mod.stemSeparate.job('track-beside-scope')?.id, scope.id);
});
