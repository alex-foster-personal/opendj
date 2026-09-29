/**
 * PARITY-09: an analyzed track with NO rekordbox mapping must reach the deck
 * with a real beatgrid, not an empty one.
 *
 * /anlz answers 200 for such a track (rb_vendor.empty_anlz_payload), so the old
 * ANALYSIS_NOT_FOUND-only gate never fired and the deck loaded with beats: [].
 * That is not just a missing wave overlay: _quantizeGrid and _requireBeatGrid
 * both read st.anlz.beatgrid.beats, so quantize did nothing, beat loops refused
 * and Beat Sync had nothing to phase-lock to. All three come alive together the
 * moment that one field carries a grid.
 *
 * Most of this drives the real upgrade against a stubbed transport (the same
 * pattern beatgrid-fallback-cache.test.mjs uses: real client, real modules,
 * only globalThis.fetch replaced). The BACKEND half of the contract - that
 * /anlz really does serve a 200 empty grid, that /rb-meta really does say
 * vendor "local", that the fallback grid really is ANLZ-shaped - is pinned
 * against the live FastAPI app in tests/webui/test_unmapped_track_beatgrid_chain.py,
 * so nothing here is asserting a shape this repo only believes in.
 *
 * Three structural guards remain, because they are about WHERE the work sits
 * and a live load needs Web Audio (same reasoning as deck-lazy-stems.test.mjs).
 *
 * MUTATION CHECK (re-measure at your SHA, do not trust this comment's count):
 *   - load() kick-off deleted                              -> fails
 *   - vendor gate bypassed (grid written anyway)           -> fails
 *   - hasAnlzBeatgrid short-circuit removed                -> fails
 *   - onSettled never invoked when the grid lands          -> fails
 *   - RbApiError branch stops checking error.code           -> fails
 *   - a non-NOT_FOUND RbApiError settles quietly            -> fails
 *   - the anlz_available check removed (fallback installed even when the
 *     response reports a real ANLZ grid now exists)          -> fails
 *   - the /anlz refetch on anlz_available skipped (deck stays gridless
 *     forever even though rekordbox now has an authoritative grid)  -> fails
 *   - hasAnlzBeatgrid(refreshed) check removed (a still-empty refetch
 *     installs as though it landed)                            -> fails
 *   - isStale() not re-checked after the /anlz refetch (a replacement load
 *     could take the refetched grid)                           -> fails
 *   - the 404 detail's anlz_available field ignored (RbApiError.body never
 *     read, so a mapping that lands with no usable apps.analysis record
 *     never triggers the /anlz refetch)                        -> fails
 * Each mutation is caught by exactly the test written for it, and by no other.
 *
 * MUTATION CHECK, structural guards (PR #765 P1 "route deferred resync
 * through the scoped scheduler", plus its Wed 2 Sep 2026 follow-up P1
 * "publish the grid inside the scheduler claim"; re-measure, do not trust
 * this comment's count):
 *   - the anlz_available refetch stops passing bypassCache=true (a browser-
 *     cached empty /anlz response would silently mask a grid that landed
 *     seconds after the initial load)                        -> fails (this file)
 *   - _clearLoadedTrackState stops routing reconcileBeforeClear through
 *     _scopedSync.run()                                       -> fails (this file)
 *   - _clearLoadedTrackState re-elects the master AFTER reconcileBeforeClear
 *     instead of before it                                    -> fails (this file)
 *   - _resyncAfterBeatgridUpgrade calls _synchronizeFollowers directly,
 *     bypassing _scopedSync.run()                           -> fails (this file)
 *   - the grid is published (st.anlz assigned) before onSettled/publish()
 *     runs, instead of inside the reclaimed scope             -> fails (this file;
 *     see "a landed grid calls onSettled(deck, true, publish) exactly once")
 *   - performance-ipc.svelte.ts's installScopedSyncRunner call skips
 *     _commandScheduler.run([deck, 'sync'], ...)              -> fails
 *     (performance-ipc.test.mjs, sibling suite)
 *   - the isStale() guard removed from the reclaimed-scope callback, or the
 *     call site's isStale closure swapped for an always-false one           -> fails
 * Each isolates to exactly the guard written for it; see git history for the
 * exact mutation diffs applied and reverted to measure this.
 *
 *   [if] the fallback fetch moves into the deck critical path [then] ⛔️
 *   [if] the upgrade stops being kicked off after the deck swap [then] ⛔️
 *   [if] an unmapped analyzed track's grid never lands on st.anlz [then] ⛔️
 *   [if] a rekordbox-mapped track's grid is replaced by ours [then] ⛔️
 *   [if] a track that already has a real grid is probed at all [then] ⛔️
 *   [if] a missing analysis row (404) kills the deck instead of settling [then] ⛔️
 *   [if] a deck swapped out mid-request takes the old track's grid [then] ⛔️
 *   [if] a settlement (landed or not) never notifies its caller [then] ⛔️
 *   [if] a real backend failure (5xx) is swallowed as though unanalyzed [then] ⛔️
 *   [if] a rejecting onSettled escapes as an unhandled rejection [then] ⛔️
 *   [if] a vendor mapping lands mid-flight and the fallback response reports
 *     anlz_available: true [then] the deck must re-fetch /anlz and adopt its
 *     now-real grid rather than the synthetic fallback, and must never stay
 *     permanently gridless if that refetch genuinely still carries none ⛔️
 *   [if] the deck is swapped out while the anlz_available refetch is still
 *     in flight [then] the replacement track must never take the old
 *     track's refetched grid ⛔️
 *   [if] /beatgrid-fallback 404s (no usable apps.analysis record) but its
 *     own detail reports anlz_available: true [then] the deck re-fetches
 *     /anlz off THIS path too, exactly like the 200 branch, rather than
 *     settling gridless because the mapping race was only handled for a
 *     successful fallback response ⛔️
 *   [if] a replacement load wins the scoped queue ahead of this attempt's
 *     deferred publish, so the deck belongs to a different track by the time
 *     publish() actually runs [then] the stale fallback grid must never
 *     overwrite the new track ⛔️
 *   [if] the deferred resync calls _synchronizeFollowers outside any scope
 *     claim, racing whatever else holds [deck, 'sync'] [then] ⛔️
 *   [if] the grid becomes observable on st.anlz before its caller has
 *     reclaimed the scope reconciliation needs [then] ⛔️
 *   [if] a replacement load wins the scoped queue ahead of this attempt's
 *     reconciliation, so the deck belongs to a different track by the time
 *     the reclaimed callback finally runs [then] the entire reconciliation
 *     (not just the grid assignment) must be skipped for the stale
 *     landed/deck pair ⛔️
 *   [if] a vendor mapping and its real PQTZ grid land while a fallback
 *     request is in flight [then] the authoritative /anlz refetch must read
 *     the network, never a browser-cached copy of the empty payload the
 *     initial load already received ⛔️
 *   [if] a playing gridless master is replaced or unloaded while gridded
 *     followers are pending on it [then] the replacement master must be
 *     elected BEFORE its stranded followers are drained and reconciled, so
 *     they are handed to the replacement instead of silently discarded
 *     against the deck that is about to stop being master ⛔️
 *   [if] reconcileBeforeClear fires after its caller has already released
 *     the deck/sync scope [then] it must reclaim [deck, sync] through the
 *     shared scheduler before touching the sync master or its followers ⛔️
 */
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadVerifiedJsonCapture } from './fixtures/verified-capture.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ENGINE = 'src/lib/rb/audio-engine.svelte.ts';
// The two settlement guard wrappers moved out of the engine into their own
// module: audio-engine.svelte.ts sits ON the file_size.max_frontend ratchet
// floor, and every reviewer-demanded guard on this surface adds lines to it.
// The behaviour asserted below is unchanged - only which file it is read from.
const GUARDS = 'src/lib/player/beatgrid-resync-guards.ts';
const API_BASE = 'https://beatgrid-upgrade.example.test';
const SID = 'a'.repeat(40);

/**
 * Real backend response bytes, not a hand-typed guess at the pydantic
 * models' shape: captured live off the FastAPI app by
 * tests/webui/test_unmapped_track_beatgrid_chain.py, whose
 * test_frontend_fixture_capture_matches_the_live_route re-asserts this exact
 * fixture against the live route on every backend test run, so a route shape
 * change reds there before this stub can silently drift out of sync.
 */
const CAPTURED = loadVerifiedJsonCapture(
	new URL('./fixtures/beatgrid-fallback-unmapped-captured.json', import.meta.url),
	new URL('./fixtures/beatgrid-fallback-unmapped-captured.manifest.json', import.meta.url)
);
const REAL_BEATS = CAPTURED.beatgrid_fallback_ok.body.beatgrid.beats;

let source;
let guardsSource;
let upgrade;
let toastHarness;
const originalFetch = globalThis.fetch;

before(async () => {
	source = readFrontendSource(ENGINE);
	guardsSource = readFrontendSource(GUARDS);
	upgrade = await loadTypeScriptModule(
		'tests/unit/fixtures/beatgrid-upgrade-analysis-source-entry.ts',
		{ viteApiBase: API_BASE }
	);
	// Separate bundle: shares one `toasts` array with the upgrade module it
	// re-exports (see the fixture's own docstring), so pushToast calls made
	// from inside upgradeDeckBeatgrid are readable here. The other tests above
	// use `upgrade` directly and never look at toasts.
	toastHarness = await loadTypeScriptModule('tests/unit/fixtures/beatgrid-upgrade-toast-entry.ts', {
		viteApiBase: API_BASE
	});
});

/** The /anlz payload a locally imported track really gets: valid, and empty. */
function emptyAnlz(stateHarness = upgrade) {
	const bands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id: SID,
		points: 38400,
		waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
		beatgrid: { beat_count: 0, beats: [] },
		// The real backend stamps this on EVERY /anlz response, unconditionally
		// (rb_assets.py `_resolve_beatgrid_source`, called on every branch of
		// `get_track_anlz`) - reading the live toggle at call time, same as the
		// production route reads it at request time, keeps this fixture honest
		// for the pre-publish source-revalidation check in beatgrid-upgrade.ts
		// (discussion_r3976638762 P1 BLOCKING).
		beatgrid_source: stateHarness.analysisSourceState.features.beatgrid,
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
}

function json(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

/** Stub transport. Records every path asked for, so "never fetched" is
 * assertable rather than assumed. The 'local'+'ok' and 'missing' bodies are
 * the CAPTURED real bytes above, not fabricated successes: only the vendor
 * override (for the rekordbox-mapped negative case, which is not this
 * requirement's scenario) and the 503 (a transport-level input this suite
 * must control directly, since no fault-injection harness produces a real
 * one) are constructed here. */
function stubDaemon({
	vendor = 'local',
	fallback = 'ok',
	anlzAvailable = false,
	anlzRefetch = 'grid'
} = {}) {
	const paths = [];
	globalThis.fetch = async (input) => {
		const path = new URL(typeof input === 'string' ? input : input.url, API_BASE).pathname;
		paths.push(path);
		if (path.endsWith('/rb-meta')) {
			if (vendor === 'local') return json(CAPTURED.rb_meta_ok.body, CAPTURED.rb_meta_ok.status);
			return json({ ...CAPTURED.rb_meta_ok.body, vendor });
		}
		if (path.endsWith('/anlz')) {
			// Only reachable via the anlz_available mid-flight refetch below - a
			// track that never claims a mapping mid-flight never hits this path.
			if (anlzRefetch === 'grid') {
				return json({ ...emptyAnlz(), beatgrid: { beat_count: REAL_BEATS.length, beats: REAL_BEATS } });
			}
			if (anlzRefetch === 'fail') {
				// A real transport failure on the refetch itself (mapping
				// disappears again, a transient 5xx) - distinct from 'empty', which
				// is a settled answer that still carries no grid.
				throw new Error('network unreachable');
			}
			return json(emptyAnlz());
		}
		if (path.endsWith('/beatgrid-fallback')) {
			if (fallback === 'missing') {
				return json(
					CAPTURED.beatgrid_fallback_not_found.body,
					CAPTURED.beatgrid_fallback_not_found.status
				);
			}
			if (fallback === 'missingButMapped') {
				// analysis.py's own real branch: no usable apps.analysis record,
				// but a vendor mapping (and real ANLZ) landed mid-flight anyway -
				// the SAME anlz_available field the 200 body carries, just inside
				// the 404's detail instead.
				return json(
					{
						detail: { ...CAPTURED.beatgrid_fallback_not_found.body.detail, anlz_available: true }
					},
					CAPTURED.beatgrid_fallback_not_found.status
				);
			}
			if (fallback === 'serverError') {
				// A REAL backend failure, distinct from the settled "not analyzed
				// yet" 404 above: apps.analysis exists but the state db could not
				// be reached. This must never be read as "no analysis yet".
				return json(
					{ detail: { code: 'STATE_DB_UNAVAILABLE', message: 'state db is locked' } },
					503
				);
			}
			// anlz_available true is a real, if rare, in-flight race: a vendor
			// mapping landed WHILE this deferred request was already sent.
			return json(
				{ ...CAPTURED.beatgrid_fallback_ok.body, anlz_available: anlzAvailable },
				CAPTURED.beatgrid_fallback_ok.status
			);
		}
		throw new Error(`unexpected request in this test: ${path}`);
	};
	return paths;
}

beforeEach(() => {
	globalThis.fetch = originalFetch;
	// PARITY-02: shouldUseBeatgridFallback now additionally requires the
	// effective 'beatgrid' selection to read 'own' (discussion_r3972682719 P1
	// BLOCKING) - default every test to that baseline so the existing
	// fallback-lands assertions below keep exercising what they always did;
	// the PARITY-02 gating tests further down set this to 'rekordbox' or
	// delete it themselves.
	upgrade.analysisSourceState.features.beatgrid = 'own';
	toastHarness.analysisSourceState.features.beatgrid = 'own';
});

// ----- behaviour: PARITY-02 gates the fallback on the effective source -----
//
// MUTATION CHECK (re-measure at your SHA, do not trust this comment's count):
//   - the effectiveSource check dropped from shouldUseBeatgridFallback -> the
//     first test below fails
//   - the pre-publish live re-check dropped from beatgrid-upgrade.ts        -> the
//     second test below fails
// Each isolates to exactly the guard written for it.

test('PARITY-02: rekordbox explicitly selected never substitutes an own-derived grid (discussion_r3972682719 P1 BLOCKING)', async () => {
	upgrade.analysisSourceState.features.beatgrid = 'rekordbox';
	const paths = stubDaemon({ vendor: 'local' });
	const st = { anlz: emptyAnlz(), anlz_error: null };

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false);

	assert.deepEqual(
		st.anlz.beatgrid.beats,
		[],
		'no own-derived grid lands while the toggle and IPC report rekordbox'
	);
	assert.deepEqual(
		paths.map((p) => p.split('/').pop()),
		['rb-meta'],
		'the fallback grid is never even fetched once rekordbox is confirmed selected'
	);
});

test('PARITY-02: a switch back to rekordbox mid-fetch is honored, not overwritten by the already-in-flight own-derived grid (discussion_r3972682719 P1 BLOCKING)', async () => {
	upgrade.analysisSourceState.features.beatgrid = 'own';
	stubDaemon({ vendor: 'local' });
	// Flip the toggle the instant the deferred /beatgrid-fallback request
	// actually goes out - simulating a DJ clicking back to rekordbox while
	// this request is in flight, which the fetchRbMeta-time gate check alone
	// cannot see.
	const inFlightFetch = globalThis.fetch;
	globalThis.fetch = async (input) => {
		const path = new URL(typeof input === 'string' ? input : input.url, API_BASE).pathname;
		if (path.endsWith('/beatgrid-fallback')) {
			upgrade.analysisSourceState.features.beatgrid = 'rekordbox';
		}
		return inFlightFetch(input);
	};
	const st = { anlz: emptyAnlz(), anlz_error: null };

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false);

	assert.deepEqual(
		st.anlz.beatgrid.beats,
		[],
		'the switch back to rekordbox must win even though the own-derived fetch already resolved'
	);
});

// ----- behaviour: the grid actually lands on the deck ------------------------

test('a mapped no-AnalysisDataPath deck with local_waveform lands the fallback grid while rbx is selected', async () => {
	upgrade.analysisSourceState.features.beatgrid = 'rekordbox';
	const paths = stubDaemon({ vendor: 'rekordbox' });
	const st = {
		anlz: { ...emptyAnlz(), local_waveform: { status: 'decoded' } },
		anlz_error: null
	};

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false);

	assert.deepEqual(st.anlz.beatgrid.beats, REAL_BEATS);
	assert.equal(st.anlz.beatgrid.beat_count, REAL_BEATS.length);
	assert.deepEqual(paths.map((p) => p.split('/').pop()), ['rb-meta', 'beatgrid-fallback']);
});

test('an unmapped analyzed track ends up with a real grid on st.anlz', async () => {
	const paths = stubDaemon({ vendor: 'local' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const before = st.anlz;

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false);

	assert.deepEqual(st.anlz.beatgrid.beats, REAL_BEATS);
	assert.equal(st.anlz.beatgrid.beat_count, REAL_BEATS.length);
	// Everything the 200 payload genuinely carried survives the merge.
	assert.deepEqual(st.anlz.waveform, before.waveform);
	assert.deepEqual(st.anlz.vocals, { status: 'not_analyzed' });
	// New object: the deck-snapshot beatgrid memo is keyed on ANLZ identity.
	assert.notEqual(st.anlz, before);
	assert.deepEqual(paths.map((p) => p.split('/').pop()), ['rb-meta', 'beatgrid-fallback']);
});

test('a vendor mapping landing mid-flight (anlz_available: true) re-fetches /anlz and installs ITS grid, never the synthetic fallback', async () => {
	// PR #765 round-3 review, Wed 2 Sep 2026: the gate at load time saw no
	// vendor mapping, but a mapping (and its real ANLZ analysis) can land on
	// the backend WHILE this deferred /beatgrid-fallback request is in
	// flight. ANLZ is always preferred, so installing the synthetic fallback
	// would bury a real PQTZ grid that already exists server-side - and
	// leaving the deck gridless until the DJ happens to reload it is just as
	// wrong, since the grid the DJ actually wants is one request away.
	const paths = stubDaemon({ vendor: 'local', anlzAvailable: true, anlzRefetch: 'grid' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed, publish) => {
		calls.push({ deck, landed });
		publish();
	});

	assert.deepEqual(st.anlz.beatgrid.beats, REAL_BEATS, 'the refetched ANLZ grid lands, never the synthetic fallback');
	assert.deepEqual(calls, [{ deck: 1, landed: true }]);
	assert.deepEqual(paths.map((p) => p.split('/').pop()), ['rb-meta', 'beatgrid-fallback', 'anlz']);
});

test('a vendor mapping landing mid-flight whose /anlz refetch still carries no grid settles gridless, not stuck retrying the synthetic fallback', async () => {
	stubDaemon({ vendor: 'local', anlzAvailable: true, anlzRefetch: 'empty' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed) => {
		calls.push({ deck, landed });
	});

	assert.deepEqual(st.anlz.beatgrid.beats, [], 'no real grid exists yet either way - never install the synthetic one over a mapped track');
	assert.deepEqual(calls, [{ deck: 1, landed: false }]);
});

test('a deck swapped out during the anlz_available refetch never takes the replacement grid', async () => {
	stubDaemon({ vendor: 'local', anlzAvailable: true, anlzRefetch: 'grid' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const before = st.anlz;
	let requests = 0;
	const inner = globalThis.fetch;
	globalThis.fetch = async (input) => {
		requests += 1;
		return inner(input);
	};

	// Stale only once the 3rd request (the /anlz refetch itself) completes -
	// rb-meta and beatgrid-fallback must both still be answered as fresh.
	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => requests >= 3);

	assert.equal(st.anlz, before, 'the isStale() guard after the refetch must fire, not just the ones before it');
});

test('onSettled is never invoked when the deck goes stale during the anlz_available refetch, even though publish() would itself no-op', async () => {
	// publish() already re-checks isStale() on its own, so a test that only
	// looks at st.anlz cannot tell this guard apart from that one - both stay
	// green if either is removed. What ONLY this guard controls is whether
	// onSettled runs at all: without it, a stale deck still gets landed:
	// true reported to its caller (audio-engine's resync hook), which would
	// phase-lock followers to a track this deck no longer holds, even though
	// the grid assignment itself later no-ops.
	stubDaemon({ vendor: 'local', anlzAvailable: true, anlzRefetch: 'grid' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	let called = false;
	let requests = 0;
	const inner = globalThis.fetch;
	globalThis.fetch = async (input) => {
		requests += 1;
		return inner(input);
	};

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => requests >= 3, () => {
		called = true;
	});

	assert.equal(called, false, 'a stale deck must never reach onSettled, not even with landed: true');
});

test('a 404 whose detail reports anlz_available: true re-fetches /anlz too, not just the 200 branch', async () => {
	// r3910924911, PR #765 4th review pass: fetchBeatgridFallback converts
	// ApiError to RbApiError, which used to keep only code/message - the same
	// anlz_available field the 200 body carries is ALSO present on THIS 404's
	// detail (apps/webui/server/routes/analysis.py's get_beatgrid_fallback: a
	// mapping can land with no usable apps.analysis record at all), and it
	// was being silently discarded in that conversion.
	const paths = stubDaemon({ vendor: 'local', fallback: 'missingButMapped', anlzRefetch: 'grid' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed, publish) => {
		calls.push({ deck, landed });
		publish();
	});

	assert.deepEqual(st.anlz.beatgrid.beats, REAL_BEATS, 'the refetched ANLZ grid lands even off the 404 path');
	assert.deepEqual(calls, [{ deck: 1, landed: true }]);
	assert.deepEqual(paths.map((p) => p.split('/').pop()), ['rb-meta', 'beatgrid-fallback', 'anlz']);
});

test('a 404 whose detail reports anlz_available: true but whose refetch still carries no grid settles gridless', async () => {
	stubDaemon({ vendor: 'local', fallback: 'missingButMapped', anlzRefetch: 'empty' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed) => {
		calls.push({ deck, landed });
	});

	assert.deepEqual(st.anlz.beatgrid.beats, []);
	assert.deepEqual(calls, [{ deck: 1, landed: false }]);
});

test('a 404-triggered /anlz refetch failure settles loudly instead of escaping as an unhandled rejection (discussion_r3920394942)', async () => {
	// The 200 branch's own anlz_available refetch runs inside the outer try, so
	// a rejection there is already caught. This one runs from INSIDE the
	// catch block (the 404 detail's own anlz_available field), so a rejecting
	// settleFromAnlzRefetch here is not covered by that same catch unless it
	// is wrapped explicitly.
	stubDaemon({ vendor: 'local', fallback: 'missingButMapped', anlzRefetch: 'fail' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	toastHarness.toasts.length = 0;
	// Must RESOLVE, not reject - callers fire this with `void`.
	await toastHarness.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed) => {
		calls.push({ deck, landed });
	});

	assert.deepEqual(st.anlz.beatgrid.beats, [], 'no grid is invented for a failed refetch');
	assert.deepEqual(calls, [{ deck: 1, landed: false }], 'onSettled must still fire so a sync master waiting on this deck is not left hanging');
	assert.equal(toastHarness.toasts.length, 1, 'a real refetch failure must reach the DJ, not settle silently');
	assert.equal(toastHarness.toasts[0].kind, 'error');
	assert.match(toastHarness.toasts[0].message, /beatgrid fallback failed/);
});

test('an ordinary 404 (anlz_available: false) never attempts an /anlz refetch', async () => {
	const paths = stubDaemon({ vendor: 'local', fallback: 'missing' });
	const st = { anlz: emptyAnlz(), anlz_error: null };

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false);

	assert.deepEqual(
		paths.map((p) => p.split('/').pop()),
		['rb-meta', 'beatgrid-fallback'],
		'a settled "not analyzed yet" 404 with no mapping must not pay for a refetch that cannot help'
	);
});

test('publish revalidates staleness at the moment it actually runs, not when onSettled was first invoked', async () => {
	// Fresh evidence after the scoped-publish fix: publish() is a closure
	// created before onSettled's caller (audio-engine.svelte.ts) claims its
	// scoped queue slot, and can run much later than the isStale() check made
	// just above it. A replacement load can win that queue and swap st.anlz
	// out from under this deck before publish() finally executes.
	stubDaemon({ vendor: 'local' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	let stale = false;
	const newTrackAnlz = emptyAnlz();

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => stale, (deck, landed, publish) => {
		// Simulate a replacement load winning the scoped queue ahead of this
		// deferred publish.
		stale = true;
		st.anlz = newTrackAnlz;
		publish();
	});

	assert.equal(st.anlz, newTrackAnlz, "the old track's fallback grid must never overwrite the new track that already won the race");
});

test('publish revalidates the effective source at the moment it actually runs, not when settle() was first called (discussion_r3973991956 P1 BLOCKING)', async () => {
	// Same deferred-publish window as the staleness test above, but the race
	// is a source switch rather than a replacement load: the live check just
	// before settle(true, publish) confirms 'own', but publish() itself is
	// queued behind onSettled's scoped command slot and can run only after a
	// switch back to rekordbox has already happened underneath it. Neither
	// isStale() (tracks loadToken, unaffected by a source switch) nor the
	// earlier live check (already evaluated) can catch this - only a re-check
	// inside the deferred thunk itself can.
	stubDaemon({ vendor: 'local' });
	const st = { anlz: emptyAnlz(), anlz_error: null };

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed, publish) => {
		// Simulate a switch to rekordbox winning the race while this deferred
		// publish sat queued behind the scoped command slot.
		upgrade.analysisSourceState.features.beatgrid = 'rekordbox';
		publish();
	});

	assert.deepEqual(
		st.anlz.beatgrid.beats,
		[],
		'the own-derived fallback grid must never land once the effective source switched to rekordbox, even if that switch happened after settle() was already called'
	);
});

test("PR #765 'Merge the fallback grid into the latest ANLZ payload': a hot-cue refresh that lands during the queue wait keeps its cues", async () => {
	// Same deferred-publish window as the test above, but the deck is NOT
	// stale: a refreshHotCues for this very track won the scoped queue first
	// and republished st.anlz with fresh cues (and no grid, so publish still
	// has work to do). The captured payload predates that refresh, so
	// assigning it wholesale would silently roll the cues back and leave the
	// wave row's markers disagreeing with the hot-cue bank.
	stubDaemon({ vendor: 'local' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const refreshedCues = [{ slot: 'A', in_ms: 1234, out_ms: null, active_loop: false, beat_loop_size: null }];

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed, publish) => {
		st.anlz = { ...st.anlz, cues: refreshedCues };
		publish();
	});

	assert.equal(st.anlz.beatgrid.beats.length > 0, true, 'the fallback grid still lands');
	assert.deepEqual(st.anlz.cues, refreshedCues, "the hot-cue refresh that ran during the queue wait must survive the grid publish");
});

test('a rekordbox-mapped track refetches ANLZ for its authoritative grid instead of settling gridless (issue #734 send-back, r3912960736)', async () => {
	// Fresh evidence beyond the mid-flight anlz_available race above: a vendor
	// mapping can also land BEFORE fetchRbMeta() itself resolves, so vendor
	// already reads 'rekordbox' the very first time this function sees it.
	// shouldUseBeatgridFallback correctly refuses the LOCAL-only synthetic
	// fallback for a mapped track - but that must not mean giving up: st.anlz
	// is still the stale local empty payload from this deck's own initial
	// load, and rekordbox now holds the authoritative grid one /anlz refetch
	// away, never the synthetic fallback a genuinely local track would get.
	const paths = stubDaemon({ vendor: 'rekordbox', anlzRefetch: 'grid' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed, publish) => {
		calls.push({ deck, landed });
		publish();
	});

	assert.deepEqual(st.anlz.beatgrid.beats, REAL_BEATS, 'the authoritative rekordbox grid lands, never a synthetic local fallback');
	assert.deepEqual(calls, [{ deck: 1, landed: true }]);
	assert.deepEqual(
		paths.map((p) => p.split('/').pop()),
		['rb-meta', 'anlz'],
		'never touches /beatgrid-fallback - that endpoint exists for LOCAL vendor tracks only'
	);
});

test('a rekordbox-mapped track whose /anlz refetch still carries no grid settles gridless, not stuck retrying the local-only fallback', async () => {
	stubDaemon({ vendor: 'rekordbox', anlzRefetch: 'empty' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed) => {
		calls.push({ deck, landed });
	});

	assert.deepEqual(st.anlz.beatgrid.beats, [], 'no real grid exists yet either way - a mapped track never gets the synthetic local fallback');
	assert.deepEqual(calls, [{ deck: 1, landed: false }]);
});

test('a track that already has a real grid is not probed at all', async () => {
	const paths = stubDaemon({ vendor: 'local' });
	const st = { anlz: { ...emptyAnlz(), beatgrid: { beat_count: 3, beats: REAL_BEATS } }, anlz_error: null };
	const before = st.anlz;

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false);

	assert.equal(st.anlz, before);
	assert.deepEqual(paths, [], 'ANLZ is always preferred, so nothing is fetched');
});

test('a missing analysis row settles quietly and leaves the deck playable', async () => {
	stubDaemon({ vendor: 'local', fallback: 'missing' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const before = st.anlz;

	// Must RESOLVE: callers fire this with `void`, so a rejection would escape
	// as an unhandled promise and a grid is never invented anyway.
	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false);

	assert.equal(st.anlz, before);
	assert.deepEqual(st.anlz.beatgrid.beats, []);
});

test('a deck swapped out mid-request never takes the old grid', async () => {
	stubDaemon({ vendor: 'local' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const before = st.anlz;
	let requests = 0;
	const inner = globalThis.fetch;
	globalThis.fetch = async (input) => {
		requests += 1;
		return inner(input);
	};

	// Stale from the second request onward: the load token moved while
	// /rb-meta was in flight.
	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => requests >= 1);

	assert.equal(st.anlz, before);
	assert.deepEqual(st.anlz.beatgrid.beats, []);
});

// ----- behaviour: the caller is told when the attempt settles, either way ---

test('a landed grid calls onSettled(deck, true, publish) exactly once, and st.anlz does not carry it until publish() runs', async () => {
	stubDaemon({ vendor: 'local' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	await upgrade.upgradeDeckBeatgrid(3, SID, st, () => false, (deck, landed, publish) => {
		calls.push({ deck, landed, gridAlreadyPublished: st.anlz.beatgrid.beats.length > 0 });
		publish();
	});

	// gridAlreadyPublished must be false: this module hands the caller a
	// `publish` thunk instead of assigning st.anlz itself specifically so a
	// caller that needs to reclaim a scope first (audio-engine.svelte.ts's
	// scoped sync runner) can do so BEFORE the grid becomes observable, not
	// after - see beatgrid-upgrade.ts's onSettled doc.
	assert.deepEqual(calls, [{ deck: 3, landed: true, gridAlreadyPublished: false }]);
	assert.deepEqual(st.anlz.beatgrid.beats, REAL_BEATS, 'publish() must still land the grid once called');
});

test('onSettled still fires (landed: false) for a rekordbox-mapped track whose authoritative grid is not ready yet either, so a sync master that never lands a grid is reported', async () => {
	// The refetch this deck triggers can itself still find no grid (rekordbox's
	// own analysis has not landed yet either) - onSettled(landed: false) must
	// still fire so a sync master relying on this deck is not left waiting
	// forever (r3912960736's refetch does not change this contract).
	stubDaemon({ vendor: 'rekordbox', anlzRefetch: 'empty' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed) => {
		calls.push({ deck, landed });
	});

	assert.deepEqual(calls, [{ deck: 1, landed: false }]);
	assert.deepEqual(st.anlz.beatgrid.beats, []);
});

test('onSettled still fires (landed: false) on a settled 404 (not analyzed yet)', async () => {
	stubDaemon({ vendor: 'local', fallback: 'missing' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed) => {
		calls.push({ deck, landed });
	});

	assert.deepEqual(calls, [{ deck: 1, landed: false }]);
});

test('onSettled still fires (landed: false) on a real backend failure (5xx)', async () => {
	stubDaemon({ vendor: 'local', fallback: 'serverError' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const calls = [];

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed) => {
		calls.push({ deck, landed });
	});

	assert.deepEqual(calls, [{ deck: 1, landed: false }]);
});

test('onSettled is never invoked when no attempt is even made (already gridded, or stale)', async () => {
	stubDaemon({ vendor: 'local' });
	let called = false;
	const onSettled = () => {
		called = true;
	};

	const already = {
		anlz: { ...emptyAnlz(), beatgrid: { beat_count: 3, beats: REAL_BEATS } },
		anlz_error: null
	};
	await upgrade.upgradeDeckBeatgrid(1, SID, already, () => false, onSettled);
	assert.equal(called, false, 'no upgrade attempt means nothing settled to report');

	const stale = { anlz: emptyAnlz(), anlz_error: null };
	await upgrade.upgradeDeckBeatgrid(1, SID, stale, () => true, onSettled);
	assert.equal(called, false, 'a deck swapped out mid-request settled for a track no longer on it');
});

test('a rejecting onSettled is contained, not an unhandled rejection', async () => {
	stubDaemon({ vendor: 'local' });
	const st = { anlz: emptyAnlz(), anlz_error: null };

	// Must RESOLVE: this is fired with `void` from the deck load path, exactly
	// like the fetches themselves - a caller's resync failure must not escape
	// as an unhandled promise either.
	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed, publish) => {
		publish();
		throw new Error('cannot phase-lock within pitch bounds');
	});

	assert.deepEqual(st.anlz.beatgrid.beats, REAL_BEATS, 'the grid still landed');
});

// ----- behaviour: a real backend failure is never read as "not analyzed" ----

test('a real backend failure (5xx) surfaces loudly, not as a settled 404', async () => {
	stubDaemon({ vendor: 'local', fallback: 'serverError' });
	const st = { anlz: emptyAnlz(), anlz_error: null };

	toastHarness.toasts.length = 0;
	await toastHarness.upgradeDeckBeatgrid(1, SID, st, () => false);

	assert.deepEqual(st.anlz.beatgrid.beats, [], 'no grid is invented for a failed fetch');
	assert.equal(toastHarness.toasts.length, 1, 'a real failure must reach the DJ, not settle quietly');
	assert.equal(toastHarness.toasts[0].kind, 'error');
	assert.match(toastHarness.toasts[0].message, /beatgrid fallback failed/);
});

test('a settled 404 (not analyzed yet) raises no toast at all', async () => {
	stubDaemon({ vendor: 'local', fallback: 'missing' });
	const st = { anlz: emptyAnlz(), anlz_error: null };

	toastHarness.toasts.length = 0;
	await toastHarness.upgradeDeckBeatgrid(1, SID, st, () => false);

	assert.equal(toastHarness.toasts.length, 0, 'not-yet-analyzed is a settled answer, not a failure');
});

// ----- structure: where the work sits ---------------------------------------

/** The body of `async load(...)` up to the point the deck is published - what
 * a DJ waits through before the deck can play. */
function criticalPath() {
	const start = source.indexOf('async load(deck: DeckId, stable_id: string, options: DeckLoadOptions = {})');
	assert.ok(start > 0, 'load() not found - this test is reading the wrong file');
	const end = source.indexOf('stages.totalBeforeSwap', start);
	assert.ok(end > start, 'totalBeforeSwap marker not found inside load()');
	return source.slice(start, end);
}

test('no rb-meta or fallback-grid fetch sits in the deck critical path', () => {
	const path = criticalPath();
	for (const banned of ['fetchRbMeta', 'fetchBeatgridFallback', 'upgradeDeckBeatgrid']) {
		assert.ok(
			!path.includes(banned),
			`${banned} is in the deck critical path; the grid upgrade is deferred so ` +
				'an unmapped track loads exactly as fast as a rekordbox one'
		);
	}
});

test('load() kicks the deferred grid upgrade off after the deck swap', () => {
	const start = source.indexOf('stages.totalBeforeSwap');
	assert.ok(start > 0, 'totalBeforeSwap marker not found');
	const afterSwap = source.slice(start, source.indexOf('async refreshHotCues', start));
	assert.ok(
		afterSwap.includes('void upgradeDeckBeatgrid('),
		'the deferred grid upgrade is never started, so an unmapped track keeps ' +
			'the empty grid /anlz served it (no quantize, no beat loops, no sync)'
	);
	// PARITY-09 finding: a grid landing mid-play on an already-engaged follower
	// must not leave it silently "synced" with no lock ever scheduled - see
	// grid-features.effectiveBeatSync, which flips true the instant st.anlz
	// carries a grid regardless of whether anything phase-locked the deck.
	const upgradeCallStart = afterSwap.indexOf('void upgradeDeckBeatgrid(');
	const upgradeCallBlock = afterSwap.slice(upgradeCallStart);
	assert.ok(
		upgradeCallBlock.includes('_beatgridGuards.afterBeatgridUpgrade'),
		'the deferred grid upgrade never resyncs a playing follower once its ' +
			'grid arrives, so setBeatSync/effectiveBeatSync can read the deck as ' +
			'synced when no lock was ever scheduled for it'
	);
});

test('the deferred resync reclaims scope through the scheduler before publishing or calling the pure resync module', () => {
	const start = guardsSource.indexOf("const afterBeatgridUpgrade: BeatgridResyncGuards['afterBeatgridUpgrade']");
	assert.ok(start > 0, 'afterBeatgridUpgrade not found - this test is reading the wrong file');
	const end = guardsSource.indexOf('\n\treturn {', start);
	assert.ok(end > start, 'end of afterBeatgridUpgrade not found');
	const body = guardsSource.slice(start, end);
	// The P1 Codex raised on PR #765: this callback fires from a `void`-fired
	// continuation well after load() already released its own [deck] scope
	// claim to the command scheduler, so calling _synchronizeFollowers
	// directly here (as an earlier revision of this fix did) races whatever
	// command now holds [deck, 'sync']. It must reclaim that scope itself,
	// and the actual sync/abandon decision must live in the pure,
	// independently tested reconcileAfterBeatgridSettled (beatgrid-resync.
	// test.mjs) rather than reinventing that logic inline here.
	assert.ok(
		body.includes('runScoped('),
		'the deferred resync calls _synchronizeFollowers outside any scope ' +
			'claim, so it can interleave with another sync-sensitive command or ' +
			'an all-scope preset phase - see AGENTS.md on the scoped command ' +
			'scheduler'
	);
	// publish()/reconcileAfterBeatgridSettled now live in a `run` helper shared
	// by both the runScoped path and the alreadyScoped inline bypass
	// (discussion_r3972154599 P1 BLOCKING: a fresh runScoped(deck, ...) here
	// when the caller already holds [deck] plus the wide barrier deadlocks
	// against that still-open outer claim), so their ordering is asserted
	// against the whole function body rather than the runScoped(...) slice
	// alone.
	assert.ok(
		body.indexOf('publish()') < body.indexOf('reconcileAfterBeatgridSettled('),
		'publish() must run BEFORE reconcileAfterBeatgridSettled, inside the ' +
			'reclaimed scope - publishing before the scope claim lets other code ' +
			'observe the grid as landed before reconciliation has run (PR #765 ' +
			'follow-up P1)'
	);
	assert.ok(
		body.includes('reconcileAfterBeatgridSettled(guardedPorts, deck, landed)'),
		'the resync hook never delegates to the pure, independently tested ' +
			'reconcileAfterBeatgridSettled - see beatgrid-resync.ts'
	);
	assert.ok(
		body.includes('if (alreadyScoped) return run();'),
		'a caller that already holds [deck] plus the wide barrier must reconcile ' +
			'inline, not through a fresh runScoped(deck, ...) claim that deadlocks ' +
			'against its own still-open outer claim (discussion_r3972154599 P1 BLOCKING)'
	);
	assert.ok(
		body.includes('guardedPorts: BeatgridResyncPorts = {') && body.includes('...ports'),
		'guardedPorts must be built by spreading the real injected ports, not a hand-rolled stand-in - ' +
			'only its mutating setters/synchronizeFollowers are wrapped with an isStale() re-check ' +
			'(discussion_r3918346693: dispose() firing while a widened reconciliation is queued or running must ' +
			'not let that continuation write sync_error/beat_sync_enabled onto a newly mounted session\'s decks)'
	);
	// discussion_r3919293432 (P1 BLOCKING): an earlier pass only guarded the
	// three port methods visible directly inside reconcileAfterBeatgridSettled's
	// own body - takePending/markSettledGridless/markPending are reachable one
	// level deeper, through the beatgrid-resync.ts internals it delegates into,
	// and were left free to drain or recreate a freshly remounted deck's own
	// resync tracking.
	for (const guardedWrite of [
		'markPending: (follower, master) => {\n\t\t\t\t\tif (!isStale())',
		'takePending: (master) => (isStale() ? [] : ports.takePending(master))',
		'markSettledGridless: (d) => {\n\t\t\t\t\tif (!isStale())'
	]) {
		assert.ok(
			body.includes(guardedWrite),
			`guardedPorts must also gate "${guardedWrite.split(':')[0]}" on isStale() - reconcileAfterBeatgridSettled ` +
				'reaches it through beatgrid-resync.ts, not directly, so it is easy to miss when only auditing this ' +
				'call site\'s own visible method calls'
		);
	}
	assert.ok(
		body.includes('isStale()') && body.indexOf('isStale()') < body.indexOf('publish()'),
		"the reclaimed scope must revalidate isStale() and skip BEFORE publish() runs - only the assignment was " +
			'guarded by the earlier publish-token fix, so a stale landed/deck pair could still reach ' +
			'reconcileAfterBeatgridSettled and phase-lock or permanently gridless-mark a replacement track ' +
			'(PR #765 re-review P1, Tue 2 Sep 2026)'
	);
});

test('the deferred resync call site threads the same load-token check into afterBeatgridUpgrade', () => {
	const start = source.indexOf('void upgradeDeckBeatgrid(deck, stable_id, st,');
	assert.ok(start > 0, 'upgradeDeckBeatgrid call site not found - this test is reading the wrong file');
	const end = source.indexOf(');', start) + 2;
	const body = source.slice(start, end);
	assert.ok(
		body.includes('_beatgridGuards.afterBeatgridUpgrade(d, landed, publish, () => token !== rt.loadToken)'),
		'the deferred resync must revalidate the SAME load-token predicate the initial fetch was gated on, not a ' +
			'fresh always-false one - otherwise a replacement load could never be detected once this callback ' +
			'finally runs behind the scoped queue'
	);
});

test('the lazy beatgrid upgrade module resolves when a deck first uses it (issue #920)', async () => {
	const lazySource = readFrontendSource('src/lib/player/beatgrid-lazy.ts');
	assert.match(
		lazySource,
		/import\('\$lib\/player\/beatgrid-upgrade'\)/,
		'beatgrid-lazy.ts must still defer-load beatgrid-upgrade for route bundle splitting'
	);
	stubDaemon();
	const lazyUpgrade = await loadTypeScriptModule(
		'tests/unit/fixtures/beatgrid-lazy-analysis-source-entry.ts',
		{ viteApiBase: API_BASE }
	);
	lazyUpgrade.analysisSourceState.features.beatgrid = 'own';
	const st = {
		anlz: { ...emptyAnlz(lazyUpgrade), beatgrid: { beat_count: 0, beats: [] } },
		anlz_error: null
	};
	await lazyUpgrade.upgradeDeckBeatgrid(1, SID, st, () => false);
	assert.deepEqual(st.anlz.beatgrid.beats, REAL_BEATS, 'the first lazy call runs the unchanged upgrade API');
});

test('the injected resync ports wire the real engine sync primitives, not stand-ins', () => {
	const start = source.indexOf('const _beatgridResyncPorts: BeatgridResyncPorts = {');
	assert.ok(start > 0, '_beatgridResyncPorts not found - this test is reading the wrong file');
	const end = source.indexOf('\n};', start);
	assert.ok(end > start, 'end of _beatgridResyncPorts object literal not found');
	const body = source.slice(start, end);
	for (const port of [
		'syncMaster: _syncMaster',
		'synchronizeFollowers: _synchronizeFollowers',
		'requiresReschedule: syncChangeRequiresReschedule',
		'deckHasRealBeatGrid(deckStates[deck])',
		'deckStates[deck].sync_error !== null',
		'deckStates[deck].sync_error = message'
	]) {
		assert.ok(
			body.includes(port),
			`_beatgridResyncPorts must wire "${port}" to the real engine primitive, ` +
				'not a stand-in - a race-condition fix that cannot see the real ' +
				'master/grid state is not a fix'
		);
	}
});

test('the mid-flight anlz_available refetch bypasses the HTTP cache instead of possibly replaying the empty response it may have cached', async () => {
	// PR #765 send-back, r3912339484: the backend served /anlz with
	// Cache-Control: public, max-age=3600 (now `private, no-cache` plus an
	// ETag, which is why this guard no longer rests on the header at all, only
	// on the explicit bypass). The initial load already fetched
	// this exact URL (with the empty local payload) minutes earlier; without
	// forcing a network read here, a browser that happens to have that
	// response cached would replay it, settling gridless despite the real
	// PQTZ grid now existing server-side.
	stubDaemon({ vendor: 'local', anlzAvailable: true, anlzRefetch: 'grid' });
	const st = { anlz: emptyAnlz(), anlz_error: null };
	const anlzInits = [];
	const inner = globalThis.fetch;
	globalThis.fetch = async (input, init) => {
		const path = new URL(typeof input === 'string' ? input : input.url, API_BASE).pathname;
		if (path.endsWith('/anlz')) anlzInits.push(init);
		return inner(input, init);
	};

	await upgrade.upgradeDeckBeatgrid(1, SID, st, () => false, (deck, landed, publish) => publish());

	assert.equal(anlzInits.length, 1, 'the authoritative refetch must actually hit /anlz');
	assert.equal(
		anlzInits[0]?.cache,
		'no-store',
		'the refetch must set cache: "no-store" so the browser HTTP cache can never satisfy it'
	);
});

test('_clearLoadedTrackState re-elects a failed/replaced master and reconciles its stranded followers through the scoped sync scheduler', () => {
	const start = source.indexOf('function _clearLoadedTrackState(st: DeckState): void {');
	assert.ok(start > 0, '_clearLoadedTrackState not found - this test is reading the wrong file');
	const end = source.indexOf('\nfunction _tempoBounds', start);
	assert.ok(end > start, '_tempoBounds marker not found after _clearLoadedTrackState');
	const body = source.slice(start, end);
	assert.ok(
		body.includes("_beatgridGuards.beforeClear(deck, stranded, 'reload')"),
		'reconcileBeforeClear is fired from load(), unload() and _recordProcessorFailure - all of which can ' +
			'release their own [deck] scope around this call - so it must reclaim [deck, "sync"] itself through ' +
			'the shared scheduler (_reconcileStrandedFollowersBeforeClear), exactly like _resyncAfterBeatgridUpgrade, ' +
			'or it can race a concurrent command on the master or a follower (issue #734 send-back r3912339491)'
	);
	// Re-pointed after 0a8631e82 "fix: unlock and resume locked paused Beat Sync MASTER
	// (DECKUX-17, #320)": the bare `_electPlayingMaster()` became a forced, reasoned
	// election. Pinning the exact call also pins `force: true`, which an unload needs.
	const electCall = "_electPlayingMaster({ force: true, reason: 'unload' })";
	const electIndex = body.indexOf(electCall);
	const reconcileIndex = body.indexOf('_beatgridGuards.beforeClear(');
	assert.ok(electIndex > 0, `${electCall} call not found inside _clearLoadedTrackState`);
	assert.ok(
		electIndex < reconcileIndex,
		"the replacement master must be elected BEFORE reconciling this deck's stranded followers - " +
			'syncMaster() derives from deckStates[_masterDeck].playing, so reconciling first still reads this ' +
			'failing/unloading deck as its own master and permanently drops its pending followers instead of ' +
			'handing them to the replacement (issue #734 send-back r3912339497)'
	);
	// Discriminator for the SECOND-pass P1: the reconciliation call defers its
	// work, so if takePending(deck) is only reachable from INSIDE that deferred
	// callback (or after clearForDeck has already wiped the pending set), the
	// whole reconciliation is dead code even though it "routes through the
	// shared scheduler" and "elects before reconciling" - both checks above
	// would stay green while every stranded follower is silently dropped. This
	// is exactly the failure verification.md warns about: a guard whose own
	// tests can't tell it apart from a no-op.
	const takePendingIndex = body.indexOf('_beatgridResyncPorts.takePending(deck)');
	const clearForDeckIndex = body.indexOf('_resyncTracking.clearForDeck(deck)');
	assert.ok(takePendingIndex > 0, 'takePending(deck) call not found inside _clearLoadedTrackState');
	assert.ok(
		takePendingIndex < reconcileIndex,
		'takePending(deck) must be called synchronously BEFORE _reconcileStrandedFollowersBeforeClear(), not from ' +
			'inside its deferred callback - the scoped claim only starts once the barrier is granted, which can be ' +
			"well after this function returns, by which point clearForDeck below has already wiped the pending set " +
			'it would be reading (issue #734 send-back r3912339491, second pass)'
	);
	assert.ok(
		takePendingIndex < clearForDeckIndex,
		'takePending(deck) must run BEFORE clearForDeck(deck) wipes the same pending set it drains'
	);
});

test('unload() elects a replacement master BEFORE reconciling its stranded followers, same as _clearLoadedTrackState', () => {
	const start = source.indexOf('async unload(deck: DeckId): Promise<void> {');
	assert.ok(start > 0, 'unload() not found - this test is reading the wrong file');
	const end = source.indexOf('\n\tsetSyncMode(deck: DeckId', start);
	assert.ok(end > start, 'setSyncMode marker not found after unload()');
	const body = source.slice(start, end);
	// Re-pointed after 0a8631e82 (DECKUX-17, #320): same reshaped election call as above.
	const electCall = "_electPlayingMaster({ force: true, reason: 'unload' })";
	const electIndex = body.indexOf(electCall);
	const reconcileIndex = body.indexOf('_beatgridGuards.beforeClear(');
	assert.ok(electIndex > 0, `${electCall} call not found inside unload()`);
	assert.ok(reconcileIndex > 0, '_beatgridGuards.beforeClear() call not found inside unload()');
	assert.ok(
		electIndex < reconcileIndex,
		'unload() must elect the replacement master BEFORE reconciling this deck\'s stranded followers, exactly ' +
			'like _clearLoadedTrackState - otherwise syncMaster() still reads this unloading deck as its own ' +
			'master and permanently drops its pending followers (issue #734 send-back Findings 5 shape, r3912339497)'
	);
	assert.equal(
		body.split('_electPlayingMaster(').length - 1,
		1,
		'unload() must elect exactly once - a second, now-redundant election call after reconciliation would ' +
			'mean this fix only added a call rather than reordering the existing one'
	);
	assert.ok(
		body.includes("_beatgridGuards.beforeClear(deck, stranded, 'unload')"),
		"unload() must route reconcileBeforeClear through the shared _reconcileStrandedFollowersBeforeClear " +
			"helper, exactly like _clearLoadedTrackState's own reconciliation call (r3912960726)"
	);
	const takePendingIndex = body.indexOf('_beatgridResyncPorts.takePending(deck)');
	assert.ok(takePendingIndex > 0, 'takePending(deck) call not found inside unload()');
	assert.ok(
		takePendingIndex < reconcileIndex,
		'takePending(deck) must be captured synchronously BEFORE _reconcileStrandedFollowersBeforeClear() defers ' +
			'its callback, same reasoning as _clearLoadedTrackState (r3912339491, second pass)'
	);
	// Electing before reconciling is not enough on its own: the election has to
	// be able to SEE that this deck is leaving. The 2s replacement wait above
	// breaks on its deadline, so a pause that never reached presentation leaves
	// st.audible true on a deck whose processor is already detached, and
	// nextPlayingMaster takes the LOWEST audible id - deck 1 re-elects itself
	// and _reconcileStrandedFollowers' `master === deck` branch drops every
	// stranded follower on the floor.
	const audibleIndex = body.indexOf('st.audible = false');
	const playingIndex = body.indexOf('st.playing = false');
	assert.ok(audibleIndex > 0, 'unload() never marks the deck non-audible before its state reset');
	assert.ok(playingIndex > 0, 'unload() never marks the deck non-playing before its state reset');
	assert.ok(
		audibleIndex < electIndex && playingIndex < electIndex,
		'unload() must mark the deck non-playing/non-audible BEFORE _electPlayingMaster(), exactly as ' +
			'_clearLoadedTrackState does - otherwise a stale audible flag surviving the replacement wait lets the ' +
			"unloading deck win its own election, and its stranded followers are dropped rather than handed to the " +
			"deck that is actually audible (PR #765 'Exclude the unloading deck before master election')"
	);
});

test('beforeClear routes through the scoped sync scheduler and guards every port write against each stranded follower\'s own identity', () => {
	const start = guardsSource.indexOf('beforeClear(deck, stranded, action) {');
	assert.ok(start > 0, 'beforeClear not found - this test is reading the wrong file');
	const end = guardsSource.indexOf('\n\t\tadoptAuthoritativeGrid(', start);
	assert.ok(end > start, 'adoptAuthoritativeGrid marker not found after beforeClear');
	const body = guardsSource.slice(start, end);
	assert.ok(
		body.includes('if (stranded.length === 0) return;'),
		// The installed scoped-sync runner (performance-ipc.svelte.ts) ignores its
		// `key` argument and always claims [...DECK_IDS, 'sync'] - the full
		// all-scope barrier, not a single-deck one. Firing it on EVERY clear, even
		// the overwhelmingly common case of zero stranded followers, needlessly
		// stalls unrelated concurrent commands on every other deck (issue #734
		// send-back r3912654116).
		'an empty stranded list must return before claiming the all-scope barrier - claiming it for an empty ' +
			'reconciliation wastes it (r3912654116)'
	);
	assert.ok(body.includes('runScoped('), 'must reclaim scope through the injected runScoped(), not fire unscoped');
	assert.ok(
		body.includes('reconcileBeforeClear(guardedPorts, deck, stranded)'),
		'must delegate to the pure, independently tested reconcileBeforeClear, passing the identity-guarded ports'
	);
	// discussion_r3914921228 (P1 BLOCKING): a stranded follower captured here
	// can itself be paused/reloaded with a brand new track before this
	// deferred reconciliation actually runs (it queues behind deck's own
	// outer claim, then behind other in-flight wide work). Every port method
	// reconcileBeforeClear can use to WRITE onto a bare follower id must
	// re-check that follower's identity immediately before writing, or a
	// replacement track inherits a disable/error/pending-mark that belongs to
	// the track it replaced.
	const strandedTokensIndex = body.indexOf('const strandedIdentities = new Map(');
	assert.ok(strandedTokensIndex > 0, 'strandedIdentities must be captured at the same instant as stranded itself');
	// discussion_r3919692507 (P1 BLOCKING): the captured identity may not be a
	// bare loadToken. dispose() installs a fresh runtime per deck whose counter
	// restarts at 0, so a number captured before a route remount compares equal
	// again after the same number of loads in the NEW session, and this
	// reconciliation would then disable or re-mark a deck it never saw. The
	// runtime OBJECT is the non-reusable half of the pair.
	assert.ok(
		body.includes('deckRuntime(follower) === captured.runtime') &&
			body.includes('deckLoadToken(follower) === captured.token'),
		'followerIsCurrent must compare the deck runtime OBJECT as well as its loadToken - a numeric token alone ' +
			'repeats across a dispose()/remount, which is exactly the identity this guard exists to distinguish ' +
			'(discussion_r3919692507)'
	);
	for (const guardedWrite of [
		'setBeatSyncEnabled: (follower, enabled) => {\n\t\t\t\t\tif (followerIsCurrent(follower))',
		'setSyncError: (follower, message) => {\n\t\t\t\t\tif (followerIsCurrent(follower))',
		'markPending: (follower, master) => {\n\t\t\t\t\tif (followerIsCurrent(follower))'
	]) {
		assert.ok(
			body.includes(guardedWrite),
			`guardedPorts must gate "${guardedWrite.split(':')[0]}" on followerIsCurrent before writing onto a ` +
				'bare follower id - the direct disable path for a gridless master (_disableForGridlessMaster) and ' +
				'the pending-mark path for a handover both write straight through this port with no other identity ' +
				'check of their own'
		);
	}
	assert.ok(
		body.includes('followers.filter(followerIsCurrent)'),
		'synchronizeFollowers must filter its follower list by identity before delegating to the real port - ' +
			"_syncStrandedFollowers's own requiresReschedule/hasRealBeatGrid filter reflects CURRENT eligibility, " +
			'not whether this is still the SAME track that was originally stranded, so a replacement track that ' +
			'happens to also have Beat Sync enabled with a valid grid would otherwise slip through'
	);
});

test('_synchronizeFollowers abandons its continuation when the engine session changed under it', () => {
	// discussion_r3919692507 (P1 BLOCKING): every guard the two settlement
	// wrappers apply is asked before _synchronizeFollowers is ENTERED, and none
	// of them survives the awaits inside it. _resumeContext() alone is an
	// open-ended wait, and everything after it reads and writes the module
	// globals by deck id, so a dispose() plus route remount landing in that
	// window let the continuation schedule transport on a new session's decks.
	const start = source.indexOf('async function _synchronizeFollowers(');
	assert.ok(start > 0, '_synchronizeFollowers not found - this test is reading the wrong file');
	const end = source.indexOf('\nconst _resyncTracking = createBeatgridResyncTracking', start);
	assert.ok(end > start, '_resyncTracking marker not found after _synchronizeFollowers');
	const body = source.slice(start, end);
	const captureIndex = body.indexOf('const session = _engineSession;');
	assert.ok(captureIndex > 0, '_synchronizeFollowers must capture the engine session once at entry');
	const awaits = [...body.matchAll(/\bawait\s+(?!Promise\.resolve)/g)].map((match) => match.index);
	assert.ok(awaits.length >= 3, `expected at least three await points, found ${awaits.length}`);
	for (const awaitIndex of awaits) {
		assert.ok(
			awaitIndex > captureIndex,
			'the session must be captured before the first await, or the capture reads the session the ' +
				'continuation already resumed into'
		);
	}
	const guards = [...body.matchAll(/session !== _engineSession/g)].map((match) => match.index);
	assert.ok(
		guards.length >= 4,
		`every await point plus the catch must re-ask the session; found only ${guards.length} guards for ` +
			`${awaits.length} awaits`
	);
	for (const awaitIndex of awaits) {
		assert.ok(
			guards.some((guardIndex) => guardIndex > awaitIndex && guardIndex - awaitIndex < 900),
			'each await must be followed by a session re-check before the next write - a guard only at entry ' +
				'is exactly the gap this finding names'
		);
	}
	assert.ok(
		body.includes('if (session !== _engineSession) throw error;'),
		'the catch must not brand a remounted session\'s decks with an error raised against the decks they replaced'
	);
	assert.ok(
		source.includes('_engineSession += 1;'),
		'dispose() must bump the session counter, or the guard above can never fire'
	);
});

test('_synchronizeFollowers gives every scheduled operation its session guard before rollback can write', () => {
	const syncStart = source.indexOf('async function _synchronizeFollowers(');
	const syncEnd = source.indexOf('\nconst _resyncTracking = createBeatgridResyncTracking', syncStart);
	assert.ok(syncStart > 0 && syncEnd > syncStart, 'cannot locate _synchronizeFollowers for the schedule guard');
	const syncBody = source.slice(syncStart, syncEnd);
	assert.ok(
		syncBody.includes('const scheduleSessionIsCurrent = (): boolean => session === _engineSession;'),
		'the batch must hand each schedule operation a predicate that remains meaningful while its processor awaits acknowledgement'
	);
	assert.ok(
		(syncBody.match(/scheduleSessionIsCurrent/g) ?? []).length >= 4,
		'every direct, blended, and re-anchored schedule in the batch must receive the session predicate, not only the batch after Promise.allSettled'
	);
	const scheduleStart = source.indexOf('async function _scheduleDeck(');
	// Re-pointed after c8d78c993 "fix: LATENCY-02 QUANTIZED LAUNCH gestures, armed glyph,
	// and master clock": the "Re-read the processor" doc comment that ended _scheduleDeck
	// is gone (the next declaration is now _observeLiveProcessorLatency), and both the
	// optimistic write and the rollback gained the armed-launch term. Same ordering pinned.
	const scheduleEnd = source.indexOf('\nasync function _observeLiveProcessorLatency(', scheduleStart);
	assert.ok(scheduleStart > 0 && scheduleEnd > scheduleStart, 'cannot locate _scheduleDeck for rollback guard');
	const scheduleBody = source.slice(scheduleStart, scheduleEnd);
	const optimisticWrite = scheduleBody.indexOf(
		'deckStates[deck].playing = _quantizedLaunchAt[deck] !== null && active ? false : active;'
	);
	const guard = scheduleBody.indexOf('if (!isCurrent()) throw new Error');
	assert.notEqual(optimisticWrite, -1, 'the optimistic playing write not found inside _scheduleDeck');
	assert.ok(
		guard >= 0 && guard < optimisticWrite,
		'the session guard must run before _scheduleDeck publishes its optimistic playing state'
	);
	assert.match(
		scheduleBody,
		/if \(isCurrent\(\)\) \{\s*deckStates\[deck\]\.playing =\s*_quantizedLaunchAt\[deck\] !== null && rt\.desiredActive \? false : rt\.desiredActive;/,
		'the rejection rollback must not write through the deck id after disposal replaced the session'
	);
});

test('switching Beat Sync off retires the deck\'s pending-follower records', () => {
	// discussion_r3919779327 (P2 BLOCKING): a follower marked pending against a
	// gridless master kept that record when the DJ switched Beat Sync off. If
	// the master was then paused before settling, syncMaster() reads null and
	// reconcileAfterBeatgridSettled's null-master branch abandons every pending
	// follower unconditionally - re-writing a sync_error onto a deck that had
	// opted out, and flipping beat_sync_enabled back to false if the DJ had
	// since re-enabled it. The record only ever described a wait this deck no
	// longer has.
	const start = source.indexOf('setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {');
	assert.ok(start > 0, 'setBeatSync not found - this test is reading the wrong file');
	const end = source.indexOf('\n\tasync setMasterTempo(', start);
	assert.ok(end > start, 'setMasterTempo marker not found after setBeatSync');
	const body = source.slice(start, end);
	const optOutIndex = body.indexOf('if (!enabled) {');
	const clearIndex = body.indexOf('_resyncTracking.clearPendingMembership(deck)');
	const firstReturnIndex = body.indexOf('return Promise.resolve();', optOutIndex);
	assert.ok(optOutIndex > 0, 'the explicit opt-out branch not found inside setBeatSync');
	assert.ok(
		clearIndex > optOutIndex && clearIndex < firstReturnIndex,
		'setBeatSync(deck, false) must clear this deck\'s pending membership inside the opt-out branch, before ' +
			'it returns - leaving the record alive lets a later gridless settlement re-error a deck that opted ' +
			'out (discussion_r3919779327)'
	);
	assert.ok(
		body.indexOf('_resyncTracking.clearPendingMembership(deck)', firstReturnIndex) === -1,
		'only the explicit opt-out may clear the pending membership - clearing it on ENABLE would discard the ' +
			'record the gridless-master retry depends on'
	);
});

test('dispose() clears beatgrid resync tracking for every deck (issue #734 send-back, r3912757819)', () => {
	const start = source.indexOf('async dispose(): Promise<void> {');
	assert.ok(start > 0, 'dispose() not found - this test is reading the wrong file');
	const end = source.indexOf('\n\tasync load(deck: DeckId', start);
	assert.ok(end > start, 'load() marker not found after dispose()');
	const body = source.slice(start, end);
	assert.ok(
		body.includes('_resyncTracking.clearForDeck(deck)'),
		'dispose() must call _resyncTracking.clearForDeck(deck) for every deck - _resyncTracking is a module-' +
			'level singleton, so without this a navigate-away/navigate-back session leaves stale ' +
			'gridlessSettled/pendingByMaster entries behind, and a new follower can read a reused deck id as ' +
			'already-settled-gridless or get reconciled against a stale pending record from the PREVIOUS session ' +
			'(r3912757819)'
	);
});
