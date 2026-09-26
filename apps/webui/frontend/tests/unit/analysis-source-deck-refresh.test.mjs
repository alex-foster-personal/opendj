/**
 * PARITY-02 rbx-vs-own analysis source toggle: refreshAnalysisSourceDecks
 * (audio-engine.svelte.ts), the fix for discussion_r3921666943 - BLOCKING "Refresh
 * decks and caches when switching source".
 *
 * Before this existed, setAnalysisSource only replaced analysisSourceState: neither
 * the shared one-fetch-per-session ANLZ cache nor a loaded deck's terminal st.anlz
 * was ever refreshed, so a source switch changed the toggle's own highlight while
 * the waveform/beatgrid kept rendering the pre-switch source.
 *
 * Runs against a REAL FastAPI server (fixtures/analysis_source_anlz_server.py) that
 * serves /anlz through the production `_resolve_beatgrid_source` function against a
 * real analysis state.db - not a fabricated `globalThis.fetch` response - per
 * discussion_r3921839834: the old version of this test could pass with the swap
 * logic itself broken, since it never ran. `/test/requests` is the server's own real
 * ASGI-middleware access log, so "no fetch happened" is read from the server's own
 * account of what it served, not from a spy on the client's fetch.
 *
 * Regression lines:
 * - if a deck with a loaded track doesn't get a fresh, cache-bypassing /anlz fetch
 *   republished onto st.anlz then broken
 * - if an unloaded deck (stable_id null) triggers a fetch anyway then broken
 * - if a deck swapped to a different track mid-request has its stale response
 *   written onto the new track's anlz then broken
 * - if the shared cache entry is published AFTER the deck's own anlz, so the
 *   authoritative-grid sink compares the new grid against a deck that already
 *   holds it, then broken (discussion_r3968213995)
 * - if the switch reports itself complete before a triggered grid
 *   reconciliation has actually settled then broken (discussion_r3970967293):
 *   a caller holding the performance command scheduler's claim could release
 *   it, and a later command's claim be granted, while a playing deck's
 *   audible reschedule was still queued
 * - if a source whose new payload has NO grid for the track fails to tell the
 *   sink so (`landed: false`) then broken - the deck must settle gridless, not
 *   silently keep beat-syncing to a grid that is gone
 * - if one track's failed fetch leaves the other decks swapped to the new
 *   source then broken (discussion_r3968214019)
 * - if two decks holding the SAME track pull the multi-MB payload twice then
 *   broken
 * - if the refresh does not report the source the server ACTUALLY served then
 *   broken (discussion_r3970117741): the caller cannot infer it, the awaits
 *   here are unbounded and the daemon can move under them
 * - if an evicted cache entry does not cause a REAL second request then broken
 *   (discussion_r3970117748), proven from the server's own access log
 * - if a deck keeps the BPM it read at load() then broken
 *   (discussion_r3969020988): `bpm` is a lane-owned projection field on the
 *   very lane this control switches, so the track row is as source-dependent
 *   as the grid is
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { stopFixtureServer } from './fixtures/stop-fixture-server.mjs';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const SERVER_SCRIPT = fileURLToPath(new URL('./fixtures/analysis_source_anlz_server.py', import.meta.url));

const SID_TRACK_A = 'real-track-a-own-grid';
const SID_TRACK_B = 'real-track-b-own-grid';
const SID_SLOW = 'real-track-slow-own-grid';
// Seeded as a real `tracks` row with NO analysis record, so the production
// route answers 200 with a real EMPTY own grid plus
// `beatgrid_own_unavailable_reason` - the grid-removing switch.
const SID_NO_OWN = 'real-track-c-no-own-analysis';
// Deliberately in neither table: the real 404 the production route raises for
// a stable_id the library no longer has, held open by the same real coroutine
// suspension SID_SLOW uses. The delay is load-bearing - see the all-or-nothing
// test for why a FAST 404 lets a partial-write implementation pass by luck.
const SID_ABSENT = 'slow-absent-track';
// RBX-lane bpm sourced from MIK, not rekordbox, and no own analysis record -
// a legitimate RBX-lane track whose tag writer merely isn't rekordbox.
const SID_MIK_BPM = 'real-track-mik-bpm-no-own-analysis';

let audio;
/** A SECOND, independent instance of the cache module (the loader bundles a
 * fresh module graph per call). audio-engine installs the real beatgrid guards
 * as the authoritative sink at its own module scope and the installer refuses a
 * second sink, so ordering is observed on an instance this file owns. */
let cache;
let serverProcess;
let apiBase;

/** Every URL the real server has served so far, oldest first.
 *
 * Callers that count DECK fetches filter for `/anlz?`: reaching a real grid
 * settlement lazily imports beatgrid-resync, which pulls vibe.svelte.ts and
 * costs one `/api/v1/settings` config GET the first time any settlement runs
 * in a session. That is a one-off module init, not a per-switch cost, and
 * counting it as a deck fetch would make this assertion say something other
 * than what its message claims. */
async function requestLog() {
	const res = await fetch(`${apiBase}/test/requests`);
	return res.json();
}

before(async () => {
	serverProcess = spawn('uv', ['run', '--no-sync', 'python', SERVER_SCRIPT], {
		cwd: REPOSITORY_ROOT,
		// PYTHONPATH, not an editable install: CI's frontend job provisions only
		// requirements.txt (no setuptools-rust build) for this one fixture, so
		// `apps` must resolve from the tree rather than from site-packages.
		env: { ...process.env, MDT_LIBRARY_MODE: 'local', PYTHONPATH: REPOSITORY_ROOT },
		stdio: ['ignore', 'pipe', 'inherit']
	});
	const port = await new Promise((resolve, reject) => {
		const rl = createInterface({ input: serverProcess.stdout });
		serverProcess.once('exit', (code) => reject(new Error(`fixture server exited early (${code})`)));
		rl.on('line', (line) => {
			const m = /^READY (\d+)$/.exec(line);
			if (m) resolve(Number(m[1]));
		});
	});
	apiBase = `http://127.0.0.1:${port}`;

	// Real PUT against the real /api/v1/analysis/source route - the same
	// endpoint AnalysisSourceToggle.svelte drives - so every /anlz this
	// server serves for the rest of the file goes through the real 'own'
	// branch of _resolve_beatgrid_source.
	const put = await fetch(`${apiBase}/api/v1/analysis/source`, {
		method: 'PUT',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ lane: 'beatgrid', toggle: 'own' })
	});
	assert.equal(put.status, 200, 'fixture server rejected the real source switch');

	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', { viteApiBase: apiBase });
	cache = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache.svelte.ts', {
		viteApiBase: apiBase
	});
	cache.installAuthoritativeAnlzGridSink((stable_id, data, landed) => {
		sinkCalls.push({
			stable_id,
			landed: landed ?? true,
			beat_count: data.beatgrid.beat_count,
			// The whole point of the ordering: snapshot what each deck held at
			// the instant the sink ran, not afterwards.
			deckAnlzAtNotify: DECKS.map((deck) => deck.anlz)
		});
		// discussion_r3970967293: lets a test prove refreshAnalysisSourceDecks
		// actually AWAITS this settlement rather than firing it and moving on.
		// `undefined` (the default) reproduces every other test's synchronous
		// sink exactly as before this hook existed.
		return sinkSettlementGate ?? undefined;
	});
});

/** Deck records for the `cache`-instance tests, indexed 1..4 by DECK_KEYS. */
const DECKS = [];
const DECK_KEYS = [1, 2, 3, 4];
let sinkCalls = [];
let sinkSettlementGate = null;

function resetCacheDecks() {
	DECKS.length = 0;
	for (const _ of DECK_KEYS) DECKS.push({ stable_id: null, anlz: null, bpm: null, key: null });
	sinkCalls = [];
	sinkSettlementGate = null;
	return Object.fromEntries(DECK_KEYS.map((key, index) => [key, DECKS[index]]));
}

after(async () => {
	if (serverProcess) await stopFixtureServer(serverProcess, apiBase);
});

beforeEach(() => {
	for (const deck of audio.DECK_IDS) {
		audio.deckStates[deck].stable_id = null;
		audio.deckStates[deck].anlz = null;
	}
});

test('refreshes a loaded deck onto a fresh, real own-source anlz payload', async () => {
	audio.deckStates[1].stable_id = SID_TRACK_A;
	audio.deckStates[1].anlz = { beatgrid: { beat_count: 4, beats: [] } };

	const before_ = await requestLog();
	await cache.refreshAnalysisSourceDecks(audio.DECK_IDS, audio.deckStates);
	const after_ = (await requestLog()).slice(before_.length).filter((url) => url.includes('/anlz?'));

	assert.equal(after_.length, 1, 'only the one loaded deck should fetch');
	assert.match(after_[0], new RegExp(`/tracks/${SID_TRACK_A}/anlz\\?(?:points=\\d+&)?gen=`));
	assert.equal(
		audio.deckStates[1].anlz.beatgrid_source,
		'own',
		'deck 1 must adopt the real own-source payload, not a leftover rekordbox one'
	);
	assert.ok(
		audio.deckStates[1].anlz.beatgrid.beat_count > 0,
		'the real own analysis for this track has downbeats; the served grid must not be empty'
	);
});

test(
	'a call already superseded at entry triggers no fetch and bumps no generation ' +
		'(discussion_r3975846958 P1 BLOCKING)',
	async () => {
		audio.deckStates[1].stable_id = SID_TRACK_A;
		audio.deckStates[1].anlz = { beatgrid: { beat_count: 4, beats: [] } };
		const genBefore = cache.currentAnlzFetchGeneration();
		const before_ = await requestLog();

		const served = await cache.refreshAnalysisSourceDecks(audio.DECK_IDS, audio.deckStates, () => true);

		const after_ = (await requestLog()).slice(before_.length);
		assert.equal(served, null);
		assert.equal(
			after_.length,
			0,
			'a call superseded before it starts must not invalidate, bump, or fetch at all - under ' +
				'sustained contention every batch pays that cost only to discard its answer later, ' +
				'and if batches consistently outlast the poll interval none can ever settle the watermark'
		);
		assert.equal(
			cache.currentAnlzFetchGeneration(),
			genBefore,
			'no generation bump for a call that never gets to do anything with it'
		);
		assert.equal(
			audio.deckStates[1].anlz.beatgrid.beat_count,
			4,
			'the deck must keep its pre-call payload untouched'
		);
	}
);

test('an unloaded deck (no stable_id) triggers no fetch and is left untouched', async () => {
	const before_ = await requestLog();
	await cache.refreshAnalysisSourceDecks(audio.DECK_IDS, audio.deckStates);
	const after_ = (await requestLog()).slice(before_.length);

	assert.equal(after_.length, 0, 'no deck has a stable_id, so the real server must see no request');
	for (const deck of audio.DECK_IDS) {
		assert.equal(audio.deckStates[deck].anlz, null);
	}
});

test('refreshes every loaded deck independently with its own real beatgrid', async () => {
	audio.deckStates[1].stable_id = SID_TRACK_A;
	audio.deckStates[2].stable_id = SID_TRACK_B;
	audio.deckStates[1].anlz = { beatgrid: { beat_count: 1, beats: [] } };
	audio.deckStates[2].anlz = { beatgrid: { beat_count: 1, beats: [] } };

	await cache.refreshAnalysisSourceDecks(audio.DECK_IDS, audio.deckStates);

	// SID_TRACK_A and SID_TRACK_B are seeded (fixtures/analysis_source_anlz_server.py)
	// with 10 and 20 real downbeats respectively - a genuinely different own grid
	// per track, not two branches of one fabricated response.
	assert.ok(audio.deckStates[1].anlz.beatgrid.beat_count > 0);
	assert.ok(audio.deckStates[2].anlz.beatgrid.beat_count > 0);
	assert.notEqual(
		audio.deckStates[1].anlz.beatgrid.beat_count,
		audio.deckStates[2].anlz.beatgrid.beat_count,
		'two tracks with different real downbeat counts must not collapse to the same grid'
	);
});

test('a deck swapped to a different track mid-request keeps the new track, not the stale response', async () => {
	audio.deckStates[1].stable_id = SID_SLOW; // server-side real 150ms delay
	audio.deckStates[1].anlz = { beatgrid: { beat_count: 1, beats: [] } };

	const refreshPromise = cache.refreshAnalysisSourceDecks(audio.DECK_IDS, audio.deckStates);
	// Simulate a real deck load landing before the analysis-source refetch settles -
	// the slow request above is still in flight against the real server.
	await new Promise((resolve) => setTimeout(resolve, 0));
	audio.deckStates[1].stable_id = 'track-c';
	const newAnlz = { beatgrid: { beat_count: 7, beats: [] } };
	audio.deckStates[1].anlz = newAnlz;

	await refreshPromise;

	assert.equal(audio.deckStates[1].stable_id, 'track-c');
	assert.equal(
		audio.deckStates[1].anlz,
		newAnlz,
		'the stale slow-track response must not overwrite the deck that has since loaded track-c'
	);
});

test(
	'a hot-cue write that lands while the staged fetch is still in flight keeps its newer cues, ' +
		'not the slower switch\'s stale ones (thread 3 fix)',
	async () => {
		const decks = resetCacheDecks();
		decks[1].stable_id = SID_SLOW; // real server-side delay, same suspension as the mid-request test above
		decks[1].anlz = { beatgrid: { beat_count: 1, beats: [] }, cues: [{ id: 'pre-existing' }] };

		const refreshPromise = cache.refreshAnalysisSourceDecks(DECK_KEYS, decks);
		// Models refreshHotCues (audio-engine.svelte.ts:3053-3054) publishing a
		// newer cue write to this SAME deck while the staged /anlz fetch above
		// is still in flight against the real server: hot_cue_save/clear/restore
		// claims `persistence-N`, not the decks-plus-sync scope this refresh
		// holds, so the two run concurrently and this write settles first.
		await new Promise((resolve) => setTimeout(resolve, 0));
		const newerCueWrite = { beatgrid: { beat_count: 1, beats: [] }, cues: [{ id: 'freshly-written' }] };
		decks[1].anlz = newerCueWrite;

		await refreshPromise;

		assert.equal(
			decks[1].anlz.cues[0].id,
			'freshly-written',
			'the slower-settling source-switch fetch must not clobber a faster hot-cue write - ' +
				'the real seeded track has no cue rows, so an unmerged overwrite would silently drop it'
		);
		assert.equal(
			decks[1].anlz.beatgrid_source,
			'own',
			'the deck must still adopt the switch\'s own beatgrid fields, not just keep the pre-switch grid'
		);
	}
);

test(
	'a hot-cue write landing on the SECOND holder of a duplicate-holder track is not clobbered by the ' +
		'first holder\'s stale anlz (thread 2 fix, discussion_r3978049105 P1 BLOCKING)',
	async () => {
		const decks = resetCacheDecks();
		// Same track on TWO decks: `holders.find` used to pick deck 1 (checked
		// first) as the sole merge base for BOTH holders and the shared cache,
		// even though the fresher write below lands on deck 3.
		decks[1].stable_id = SID_SLOW; // real server-side delay, same suspension used above
		decks[3].stable_id = SID_SLOW;
		decks[1].anlz = { beatgrid: { beat_count: 1, beats: [] }, cues: [{ id: 'pre-existing' }] };
		decks[3].anlz = { beatgrid: { beat_count: 1, beats: [] }, cues: [{ id: 'pre-existing' }] };

		const refreshPromise = cache.refreshAnalysisSourceDecks(DECK_KEYS, decks);
		// Models refreshHotCues (audio-engine.svelte.ts) publishing a newer cue
		// write for deck 3 - NOT deck 1, the holder `holders.find` would pick -
		// while the staged /anlz fetch above is still in flight. Unlike the
		// single-deck case above, refreshHotCues always ALSO calls
		// refreshAnlzCacheEntry, so the shared cache carries this write too.
		await new Promise((resolve) => setTimeout(resolve, 0));
		const newerCueWrite = { beatgrid: { beat_count: 1, beats: [] }, cues: [{ id: 'freshly-written-on-holder-3' }] };
		decks[3].anlz = newerCueWrite;
		cache.refreshAnlzCacheEntry(SID_SLOW, newerCueWrite);

		await refreshPromise;

		assert.equal(
			decks[3].anlz.cues[0].id,
			'freshly-written-on-holder-3',
			'the holder that actually received the fresh write must keep it, not a copy of holder 1\'s stale anlz'
		);
		assert.equal(
			decks[1].anlz.cues[0].id,
			'freshly-written-on-holder-3',
			'the OTHER holder must adopt the freshest known payload too, not stay on its own pre-switch cues'
		);
	}
);

// --------------------------------------------------- cache-before-deck order

test('the grid sink is notified while every deck still holds its PRE-switch anlz', async () => {
	const decks = resetCacheDecks();
	const preSwitch = { beatgrid: { beat_count: 4, beats: [] } };
	decks[1].stable_id = SID_TRACK_A;
	decks[1].anlz = preSwitch;

	await cache.refreshAnalysisSourceDecks(DECK_KEYS, decks);

	assert.equal(sinkCalls.length, 1, 'the one loaded track must reach the authoritative grid sink');
	assert.equal(sinkCalls[0].stable_id, SID_TRACK_A);
	assert.equal(
		sinkCalls[0].deckAnlzAtNotify[0],
		preSwitch,
		'the deck was written before the cache, so sameBeatgrid would see the new grid on both ' +
			'sides and skip reconcileAfterBeatgridSettled - a playing Beat-Synced deck keeps the old grid'
	);
	assert.ok(sinkCalls[0].beat_count > 0, 'the sink must carry the real own grid, not an empty one');
	assert.equal(decks[1].anlz.beatgrid_source, 'own', 'the deck still has to end up on the new payload');
	assert.notEqual(decks[1].anlz, preSwitch);
});

test('the switch does not resolve until the grid sink settlement it triggered has settled', async () => {
	// discussion_r3970967293: adoptAuthoritativeGrid's real implementation
	// starts the audible reschedule (afterBeatgridUpgrade -> runScoped's widen)
	// and used to be fire-and-forget from here, so a caller holding the
	// performance command scheduler's claim could report the switch complete,
	// release the claim, and let a LATER command be granted while that
	// reschedule was still queued. This proves the opposite: the promise this
	// function returns stays pending for as long as the sink's own settlement
	// promise does.
	const decks = resetCacheDecks();
	decks[1].stable_id = SID_TRACK_A;
	decks[1].anlz = { beatgrid: { beat_count: 4, beats: [] } };

	let releaseSink;
	sinkSettlementGate = new Promise((resolve) => {
		releaseSink = resolve;
	});

	let settled = false;
	const refreshPromise = cache.refreshAnalysisSourceDecks(DECK_KEYS, decks).then(() => {
		settled = true;
	});

	// Wait for the sink to actually have been CALLED (real network fetch and
	// cache write both done) rather than counting microtasks: the fetch below
	// is a real HTTP round trip against the local fixture server, which takes
	// far longer than a handful of microtask turns, so a fixed microtask count
	// would pass this assertion for the trivial reason that the function
	// hasn't reached the sink yet, mutated or not. Polling on the sink's own
	// call record is what makes this a control ON the reconciliation-await
	// specifically.
	const deadline = Date.now() + 5000;
	while (sinkCalls.length === 0) {
		if (Date.now() > deadline) throw new Error('sink was never called; fixture request did not complete');
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
	assert.equal(settled, false, 'the switch must not report done while the sink settlement is still pending');

	releaseSink();
	await refreshPromise;
	assert.equal(settled, true, 'and it must resolve once the sink settlement actually does');
});

test('two decks on the SAME track fetch once and reconcile through one sink call', async () => {
	const decks = resetCacheDecks();
	decks[1].stable_id = SID_TRACK_A;
	decks[3].stable_id = SID_TRACK_A;

	const before_ = await requestLog();
	await cache.refreshAnalysisSourceDecks(DECK_KEYS, decks);
	const all_ = (await requestLog()).slice(before_.length);
	const after_ = all_.filter((url) => url.includes('/anlz?'));

	assert.equal(after_.length, 1, 'one track means one multi-MB payload, however many decks hold it');
	assert.equal(
		all_.filter((url) => /\/tracks\/[^/]+$/.test(url)).length,
		1,
		'the lane-owned row is one fetch per track too'
	);
	assert.equal(sinkCalls.length, 1, 'a second sink call would schedule a duplicate settlement');
	assert.equal(decks[1].anlz, decks[3].anlz, 'both decks take the same fresh payload');
});

// ------------------------------------------------- grid REMOVED by the switch

test('a switch that removes the grid tells the sink landed:false rather than staying silent', async () => {
	const decks = resetCacheDecks();
	decks[2].stable_id = SID_NO_OWN;
	decks[2].anlz = { beatgrid: { beat_count: 8, beats: [] } };

	await cache.refreshAnalysisSourceDecks(DECK_KEYS, decks);

	assert.equal(sinkCalls.length, 1, 'losing the grid must still reach the sink; silence strands the deck');
	assert.equal(sinkCalls[0].landed, false);
	assert.equal(sinkCalls[0].beat_count, 0, 'the real route serves an explicitly empty own grid here');
	assert.equal(
		decks[2].anlz.beatgrid_own_unavailable_reason,
		'no own beatgrid record for this track yet',
		'the honest reason from the real route must survive onto the deck'
	);
});

// ------------------------------------------------------------- all-or-nothing

test('one track failing leaves EVERY deck on the old source, not a split fleet', async () => {
	const decks = resetCacheDecks();
	const goodPreSwitch = { beatgrid: { beat_count: 4, beats: [] } };
	const badPreSwitch = { beatgrid: { beat_count: 5, beats: [] } };
	decks[1].stable_id = SID_TRACK_A; // real 200
	// A real 404 from the production route, delayed 150ms server-side so deck
	// 1's success lands FIRST. Publishing per-deck inside the Promise.all - the
	// shape this replaced - would therefore have already swapped deck 1 by the
	// time the rejection arrives, which is the split fleet being ruled out.
	decks[2].stable_id = SID_ABSENT;
	decks[1].anlz = goodPreSwitch;
	decks[2].anlz = badPreSwitch;

	await assert.rejects(
		() => cache.refreshAnalysisSourceDecks(DECK_KEYS, decks),
		'a failed member must reject, never resolve with a partial swap'
	);

	assert.equal(decks[1].anlz, goodPreSwitch, 'the deck whose fetch SUCCEEDED must not have moved');
	assert.equal(decks[2].anlz, badPreSwitch);
	assert.equal(sinkCalls.length, 0, 'no deck reconciled, so no grid settlement may have been scheduled');
	assert.equal(
		cache.getAnlzEntry(SID_TRACK_A),
		undefined,
		'the shared cache must read as never-requested too, so the retry refetches under the new source'
	);
});

// -------------------------------------------- lane-owned track row on a switch

/** Flip the DAEMON's selection through the real endpoint, without the module
 * under test. */
async function daemonSelect(toggle) {
	await daemonSelectLane('beatgrid', toggle);
}

async function daemonSelectLane(lane, toggle) {
	const res = await fetch(`${apiBase}/api/v1/analysis/source`, {
		method: 'PUT',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ lane, toggle })
	});
	assert.equal(res.status, 200, `fixture server rejected the direct daemon switch of ${lane}`);
}

/** Poll until a real fixture-server /anlz fetch has published `ready`, rather
 * than sleeping a guessed duration: a fixed sleep races a slow server under
 * pool load (issue #1820). */
async function _waitForAnlzReady(stableId, label = stableId) {
	const deadline = Date.now() + 5000;
	for (;;) {
		if (cache.getAnlzEntry(stableId)?.status === 'ready') return;
		if (Date.now() > deadline) {
			throw new Error(`anlz entry for ${label} never reached ready`);
		}
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

async function rowBpm(stableId) {
	const res = await fetch(`${apiBase}/api/v1/tracks/${stableId}`);
	assert.equal(res.status, 200, `the real tracks route did not serve ${stableId}`);
	return (await res.json()).bpm ?? null;
}

test('a switch re-reads the lane-owned track row, so the displayed BPM cannot lag the grid', async () => {
	const decks = resetCacheDecks();
	decks[1].stable_id = SID_TRACK_A;
	decks[1].anlz = { beatgrid: { beat_count: 4, beats: [] } };
	// What load() captured. 174 is not what either lane serves, so it can only
	// survive by never being re-read.
	decks[1].bpm = 174;
	decks[1].key = 'stale';

	// Pin the KEY lane to rbx explicitly. STANDALONE-06 (#3926) made an
	// UNTOGGLED lane default to own for a track with no rekordbox mapping, and
	// every fixture track is unmapped with no own key record, so an unset key
	// lane now serves an honest null. The key assertions below need a real key
	// to replace the stale one with; an explicit toggle wins over that default,
	// which restores the key-lane conditions this test was written against.
	await daemonSelectLane('key', 'rbx');
	try {
		await assertSwitchReReadsTrackRow(decks);
	} finally {
		// A failed assertion must not leave the shared daemon retuned for every
		// later test in this file.
		await daemonSelectLane('key', 'unset');
		await daemonSelect('own');
	}
});

async function assertSwitchReReadsTrackRow(decks) {
	// The daemon has been on OWN since before() (a real PUT through the real
	// endpoint), and this track has no own projection row, so the read model
	// answers with own's honest absence rather than silently serving the
	// rekordbox tag. That IS the post-switch truth the deck has to show.
	const before_ = await requestLog();
	await cache.refreshAnalysisSourceDecks(DECK_KEYS, decks);
	const rowFetches = (await requestLog())
		.slice(before_.length)
		.filter((url) => /\/tracks\/[^/]+$/.test(url));
	const ownBpm = await rowBpm(SID_TRACK_A);

	assert.equal(rowFetches.length, 1, 'the deck must actually re-read GET /tracks/{id}');
	assert.match(rowFetches[0], new RegExp(`/tracks/${SID_TRACK_A}$`));
	assert.equal(decks[1].bpm, ownBpm, 'the deck must mirror the row the route serves right now');
	assert.notEqual(
		decks[1].bpm,
		174,
		'DeckHeader and PerformanceState.bpm kept the tempo captured at load() while effective_bpm moved'
	);

	// Control, in the other direction, and the proof the field really is
	// source-dependent rather than constant: flip the daemon back to rbx and
	// refresh again.
	await daemonSelect('rbx');
	await cache.refreshAnalysisSourceDecks(DECK_KEYS, decks);
	const rbxBpm = await rowBpm(SID_TRACK_A);
	assert.notEqual(
		rbxBpm,
		ownBpm,
		'if both lanes served the same BPM for this track then this test proves nothing'
	);
	assert.equal(decks[1].bpm, rbxBpm, 'switching back must bring the rekordbox tag BPM with it');
	// `key` is the OTHER lane-owned field a deck holds. Its lane is not
	// switchable from this control, so the two directions agree on it by
	// construction - what matters is that a stale one is replaced at all.
	const rbxRow = await (await fetch(`${apiBase}/api/v1/tracks/${SID_TRACK_A}`)).json();
	assert.equal(rbxRow.key, '8A', 'the fixture must serve a real key, or the next assertion is vacuous');
	assert.equal(decks[1].key, rbxRow.key, 'a stale lane-owned key must be replaced from the fresh row');
}

test('a failed track-row read is as total as a failed grid read', async () => {
	const decks = resetCacheDecks();
	decks[1].stable_id = SID_TRACK_A;
	decks[1].bpm = 174;
	decks[2].stable_id = SID_ABSENT;

	await assert.rejects(() => cache.refreshAnalysisSourceDecks(DECK_KEYS, decks));

	assert.equal(decks[1].bpm, 174, 'the succeeding deck must not have moved its BPM either');
	assert.equal(decks[1].anlz, null);
});

// ------------------- the served source, and cache invalidation, for real -----

test('the refresh REPORTS the source the server actually served, not the one asked for', async () => {
	const decks = resetCacheDecks();
	decks[1].stable_id = SID_TRACK_A;

	try {
		await daemonSelect('own');
		assert.equal(
			await cache.refreshAnalysisSourceDecks(DECK_KEYS, decks),
			'own',
			'read off the real payload\'s beatgrid_source, which the production route stamps'
		);

		await daemonSelect('rbx');
		assert.equal(await cache.refreshAnalysisSourceDecks(DECK_KEYS, decks), 'rekordbox');
	} finally {
		// A failed assertion above must not leave the shared daemon on rbx and
		// silently retune every later test in this file.
		await daemonSelect('own');
	}
});

test('a non-rekordbox RBX-lane bpm writer (MIK) is not mistaken for own, so the pairing guard does not false-positive (discussion_r3974235445 P1 BLOCKING)', async () => {
	const decks = resetCacheDecks();
	decks[1].stable_id = SID_MIK_BPM;

	try {
		await daemonSelect('rbx');
		// Before the fix, `bpmOnOwn` read "not literally rekordbox" as "own",
		// disagreed with the real rekordbox-selected grid, and this threw the
		// mid-refresh pairing-guard error even though nothing raced at all -
		// the tag was always mik and the grid was always rekordbox, both
		// honestly reported for the SAME, single, unchanging selection.
		const served = await cache.refreshAnalysisSourceDecks(DECK_KEYS, decks);
		assert.equal(served, 'rekordbox', 'the rbx-selected grid must still be served');
	} finally {
		await daemonSelect('own');
	}
});

test('no loaded deck reports NULL, which is not the same claim as "rekordbox"', async () => {
	const decks = resetCacheDecks();

	const before_ = await requestLog();
	const served = await cache.refreshAnalysisSourceDecks(DECK_KEYS, decks);
	const after_ = (await requestLog()).slice(before_.length);

	assert.equal(after_.length, 0, 'nothing was loaded, so the real server must see no request');
	assert.equal(served, null, 'nothing was served, so there is no served source to report');
});

test('invalidateAllAnlzCacheEntries forces a REAL second request, not a fabricated one', async () => {
	// PARITY-02: an analysis source switch changes what beatgrid a fresh /anlz
	// carries for EVERY track, not just whichever ones are cached right now --
	// discussion_r3921666943 (setAnalysisSource never evicted this cache at
	// all). A per-id invalidateAnlzCacheEntry loop from the caller would still
	// miss a track reselected later without ever having been cached at switch
	// time, so this must clear the whole map at once, not one key.
	//
	// discussion_r3970117748: this regression previously lived in
	// anlz-cache.test.mjs behind a `globalThis.fetch` stub, so its
	// post-invalidation assertion counted calls into a manufactured payload and
	// could pass with the production request, source selection and parser all
	// broken. Here `ensureAnlz` reaches the real /anlz route on the real fixture
	// server, and the proof that it refetched is the SERVER's own access log.
	resetCacheDecks();
	const before_ = await requestLog();
	cache.ensureAnlz(SID_TRACK_A);
	cache.ensureAnlz(SID_TRACK_B);
	await _waitForAnlzReady(SID_TRACK_A);
	await _waitForAnlzReady(SID_TRACK_B);

	const entryA = cache.getAnlzEntry(SID_TRACK_A);
	assert.equal(entryA.status, 'ready', 'the real route must have answered before this asserts');
	assert.ok(
		entryA.data.beatgrid.beat_count > 0,
		'a real own grid came back through the real parser, not an empty stand-in'
	);
	const firstPass = (await requestLog()).slice(before_.length).filter((url) => url.includes('/anlz?'));
	assert.equal(firstPass.length, 2, 'two distinct tracks, two real requests');

	cache.invalidateAllAnlzCacheEntries();
	assert.equal(cache.getAnlzEntry(SID_TRACK_A), undefined, 'an evicted entry reads as never-requested');
	assert.equal(cache.getAnlzEntry(SID_TRACK_B), undefined, 'evicting one must not leave a sibling stale');

	const midpoint = await requestLog();
	cache.ensureAnlz(SID_TRACK_A);
	await _waitForAnlzReady(SID_TRACK_A);
	const secondPass = (await requestLog()).slice(midpoint.length).filter((url) => url.includes('/anlz?'));

	assert.equal(secondPass.length, 1, 'ensureAnlz must treat an evicted entry as a real cache miss');
	assert.match(secondPass[0], new RegExp(`/tracks/${SID_TRACK_A}/anlz\\?(?:points=\\d+&)?gen=`));
	assert.equal(cache.getAnlzEntry(SID_TRACK_A).status, 'ready');
});

test('evictAnlzCacheEntriesServingOtherSource evicts only entries whose beatgrid_source disagrees with the wanted one (discussion_r3973991969 P1 BLOCKING)', async () => {
	// A source switch with no LOADED deck to disagree (analysis-source.svelte.ts's
	// _decksDisagreeWith) never runs refreshAnalysisSourceDecks, so a track merely
	// prefetched by library browsing keeps whatever beatgrid_source it was fetched
	// under - the next deck that loads it must not get a cache HIT on those stale
	// bytes just because nothing was loaded at switch time. Moved off a
	// fabricated globalThis.fetch onto this file's real fixture server per
	// discussion_r3974993960 (P1 BLOCKING): the old version could pass with the
	// production /anlz route, beatgrid_source resolution, and parser all broken.
	//
	// Full invalidation, not just these two ids: eviction sweeps the WHOLE
	// shared cache, and an earlier test in this file can leave an unrelated
	// track (SID_MIK_BPM, SID_SLOW, ...) cached under 'rekordbox' - which
	// would make this test's "true" assertion pass for the wrong reason.
	cache.invalidateAllAnlzCacheEntries();
	try {
		await daemonSelect('own');
		cache.ensureAnlz(SID_TRACK_A);
		await _waitForAnlzReady(SID_TRACK_A, 'the own-sourced fetch must land first');

		await daemonSelect('rbx');
		cache.ensureAnlz(SID_TRACK_B);
		await _waitForAnlzReady(SID_TRACK_B, 'the rbx-sourced fetch must land too');

		const evicted = cache.evictAnlzCacheEntriesServingOtherSource('own');

		assert.equal(evicted, true, 'a disagreeing entry was cached, so eviction must report it happened');
		assert.equal(
			cache.getAnlzEntry(SID_TRACK_B),
			undefined,
			'the pre-switch rekordbox entry must be evicted so the next deck load actually refetches under own'
		);
		assert.equal(
			cache.getAnlzEntry(SID_TRACK_A)?.status,
			'ready',
			'an entry that already agrees with the wanted source must be left alone'
		);
	} finally {
		await daemonSelect('own');
	}
});

test('evictAnlzCacheEntriesServingOtherSource reports nothing evicted when every cached entry already agrees (discussion_r3973991969 P1 BLOCKING)', async () => {
	// An empty cache or a permanently broken eviction function would also
	// report `false` here - the control below (a real disagreeing entry,
	// evicted in the SAME call) is what proves this `false` means "nothing
	// disagreed" rather than "this function reports nothing, ever". Full
	// invalidation (not just these two ids), for the same reason as the
	// preceding test: eviction sweeps the WHOLE shared cache.
	cache.invalidateAllAnlzCacheEntries();
	try {
		await daemonSelect('own');
		cache.ensureAnlz(SID_TRACK_A);
		await _waitForAnlzReady(SID_TRACK_A);

		const evicted = cache.evictAnlzCacheEntriesServingOtherSource('own');

		assert.equal(
			evicted,
			false,
			'nothing disagreed, so the caller must not bump the fetch generation for no reason'
		);
		assert.equal(cache.getAnlzEntry(SID_TRACK_A)?.status, 'ready');

		// Control: the same cache, same call shape, but now WITH a disagreeing
		// entry - proves `false` above was a real report, not this function's
		// only possible answer.
		await daemonSelect('rbx');
		cache.invalidateAnlzCacheEntry(SID_TRACK_B);
		cache.ensureAnlz(SID_TRACK_B);
		await _waitForAnlzReady(SID_TRACK_B);
		assert.equal(
			cache.evictAnlzCacheEntriesServingOtherSource('own'),
			true,
			'control: a genuinely disagreeing entry must still be reported'
		);
	} finally {
		await daemonSelect('own');
	}
});
