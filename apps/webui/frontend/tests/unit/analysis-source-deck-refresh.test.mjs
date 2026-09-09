/**
 * PARITY-02 rbx-vs-own analysis source toggle: engine.refreshDecksForAnalysisSourceChange
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
 * - if a source whose new payload has NO grid for the track fails to tell the
 *   sink so (`landed: false`) then broken - the deck must settle gridless, not
 *   silently keep beat-syncing to a grid that is gone
 * - if one track's failed fetch leaves the other decks swapped to the new
 *   source then broken (discussion_r3968214019)
 * - if two decks holding the SAME track pull the multi-MB payload twice then
 *   broken
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

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
	});
});

/** Deck records for the `cache`-instance tests, indexed 1..4 by DECK_KEYS. */
const DECKS = [];
const DECK_KEYS = [1, 2, 3, 4];
let sinkCalls = [];

function resetCacheDecks() {
	DECKS.length = 0;
	for (const _ of DECK_KEYS) DECKS.push({ stable_id: null, anlz: null });
	sinkCalls = [];
	return Object.fromEntries(DECK_KEYS.map((key, index) => [key, DECKS[index]]));
}

after(() => {
	serverProcess?.kill();
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
	await audio.engine.refreshDecksForAnalysisSourceChange();
	const after_ = (await requestLog()).slice(before_.length).filter((url) => url.includes('/anlz?'));

	assert.equal(after_.length, 1, 'only the one loaded deck should fetch');
	assert.match(after_[0], new RegExp(`/tracks/${SID_TRACK_A}/anlz\\?points=`));
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

test('an unloaded deck (no stable_id) triggers no fetch and is left untouched', async () => {
	const before_ = await requestLog();
	await audio.engine.refreshDecksForAnalysisSourceChange();
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

	await audio.engine.refreshDecksForAnalysisSourceChange();

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

	const refreshPromise = audio.engine.refreshDecksForAnalysisSourceChange();
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

test('two decks on the SAME track fetch once and reconcile through one sink call', async () => {
	const decks = resetCacheDecks();
	decks[1].stable_id = SID_TRACK_A;
	decks[3].stable_id = SID_TRACK_A;

	const before_ = await requestLog();
	await cache.refreshAnalysisSourceDecks(DECK_KEYS, decks);
	const after_ = (await requestLog()).slice(before_.length);

	assert.equal(after_.length, 1, 'one track means one multi-MB payload, however many decks hold it');
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
		'no own analysis for this track',
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
