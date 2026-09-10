import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://anlz-cache.example.test';

/** Real wall-clock wait for a test proving the ambient retry timer (source's
 * RETRYABLE_COOLDOWN_MS = 2000) actually fires once. 400ms of slack over the
 * cooldown - comfortably past normal single-digit-ms setTimeout jitter, but
 * deliberately NOT close to double the cooldown: a wait that can span a
 * SECOND retry cycle makes a test asserting "exactly one fetch so far"
 * flaky in the other direction (measured: a 5000ms wait let two retries
 * land inside one window, so `calls` was already 3 where the test expected
 * to still observe 2). */
const RETRY_FIRES_WAIT_MS = 2400;

let cache;
const originalFetch = globalThis.fetch;

before(async () => {
	cache = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache.svelte.ts', {
		viteApiBase: API_BASE
	});
});

// A retryable publish schedules a real ~2s ambient-retry timer (see
// anlz-cache.svelte.ts's _publishAnlzResult). Left to fire after its own
// test ends, it calls ensureAnlz mid-LATER-test against whatever fetch mock
// (or none) is active then - observed live as a stray "TypeError: fetch
// failed" attributed to the wrong test. The module keeps no test-only reset
// hook (that would be a 6th instance of this codebase's already-at-ratchet
// `_reset*ForTests` unused-export pattern), so intercept every real
// setTimeout call from the test side instead and cancel whatever is still
// pending once each test ends - this needs no cooperation from the module
// under test, source-side or otherwise.
const _pendingTimers = new Set();
const originalSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (...args) => {
	const id = originalSetTimeout(...args);
	_pendingTimers.add(id);
	return id;
};

afterEach(() => {
	for (const id of _pendingTimers) clearTimeout(id);
	_pendingTimers.clear();
});

function anlzPayload(local_waveform, cues = []) {
	return {
		stable_id: 'abc',
		points: 38400,
		waveform: { kind: 'mono', preview: { length: 0, low: [], mid: [], high: [] }, detail: { length: 0, low: [], mid: [], high: [] } },
		beatgrid: { beat_count: 0, beats: [] },
		cues,
		phrases: [],
		local_waveform,
		vocals: { status: 'not_analyzed' }
	};
}

function jsonResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

test('a retryable not_decoded (saturated decoder) refetches only after its cooldown elapses', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(
			anlzPayload({
				status: 'not_decoded',
				reason: 'decoder saturated',
				preview_b64: null,
				preview_max: null,
				retryable: true
			})
		);
	};
	const originalNow = globalThis.performance.now;
	try {
		cache.ensureAnlz('retryable-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 1);
		assert.equal(cache.getAnlzEntry('retryable-track').status, 'ready');

		// Immediately after: still inside the cooldown, must NOT refetch -
		// this is the loop Codex flagged (a reactive $effect rerunning at
		// full speed against a saturated decoder).
		cache.ensureAnlz('retryable-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 1, 'a retryable entry must not be hammered inside its cooldown');

		// Once the cooldown has genuinely elapsed, retrying must fire again.
		globalThis.performance.now = () => originalNow.call(globalThis.performance) + 10_000;
		cache.ensureAnlz('retryable-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 2, 'a retryable entry must refetch once its cooldown has passed');
	} finally {
		globalThis.fetch = originalFetch;
		globalThis.performance.now = originalNow;
	}
});

test('a permanent not_decoded (no ffmpeg, missing file) never refetches', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(
			anlzPayload({
				status: 'not_decoded',
				reason: 'ffmpeg is not installed',
				preview_b64: null,
				preview_max: null
			})
		);
	};
	try {
		cache.ensureAnlz('permanent-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 1);

		cache.ensureAnlz('permanent-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 1, 'a permanent not_decoded is terminal - retrying cannot help');
	} finally {
		globalThis.fetch = originalFetch;
	}
});

test('isAnlzEntryUsable rejects a retryable ready entry, even after its cooldown', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(
			anlzPayload({
				status: 'not_decoded',
				reason: 'decoder saturated',
				preview_b64: null,
				preview_max: null,
				retryable: true
			})
		);
	};
	const originalNow = globalThis.performance.now;
	try {
		cache.ensureAnlz('deck-load-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		const entry = cache.getAnlzEntry('deck-load-track');
		assert.equal(entry.status, 'ready');

		// This is the exact check audio-engine.svelte.ts uses to decide
		// whether a deck load can skip /anlz and reuse the cache. A retryable
		// entry must never satisfy it - not during the cooldown, and not
		// after it, since nothing else schedules the promised retry and a
		// deck load treating this as a hit would pin the empty payload to
		// the deck indefinitely (issue #735 follow-up, Codex finding
		// discussion_r3907339824).
		assert.equal(cache.isAnlzEntryUsable(entry), false, 'must not be usable inside the cooldown');

		globalThis.performance.now = () => originalNow.call(globalThis.performance) + 10_000;
		const staleEntry = cache.getAnlzEntry('deck-load-track');
		assert.equal(
			cache.isAnlzEntryUsable(staleEntry),
			false,
			'must not be usable after cooldown either - the caller must re-fetch, not reuse'
		);
	} finally {
		globalThis.fetch = originalFetch;
		globalThis.performance.now = originalNow;
	}
});

test('isAnlzEntryUsable accepts a terminal ready entry (decoded or permanently not_decoded)', async () => {
	globalThis.fetch = async () =>
		jsonResponse(
			anlzPayload({
				status: 'decoded',
				reason: null,
				preview_b64: 'AAAA',
				preview_max: 200
			})
		);
	try {
		cache.ensureAnlz('usable-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		const entry = cache.getAnlzEntry('usable-track');
		assert.equal(cache.isAnlzEntryUsable(entry), true);
	} finally {
		globalThis.fetch = originalFetch;
	}
});

test('isAnlzEntryUsable rejects undefined and non-ready entries', () => {
	assert.equal(cache.isAnlzEntryUsable(undefined), false);
	assert.equal(cache.isAnlzEntryUsable({ status: 'loading' }), false);
	assert.equal(cache.isAnlzEntryUsable({ status: 'error', code: 'FETCH_FAILED' }), false);
});

test('fetchAnlzForDeckLoad returns and publishes a terminal answer on the first fetch', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(
			anlzPayload({ status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 })
		);
	};
	try {
		const result = await cache.fetchAnlzForDeckLoad('deck-direct-terminal');
		assert.equal(calls, 1);
		assert.equal(result.local_waveform.status, 'decoded');
		const published = cache.getAnlzEntry('deck-direct-terminal');
		assert.equal(published.status, 'ready');
		assert.equal(
			cache.isAnlzEntryUsable(published),
			true,
			'the terminal result must be published into the shared cache'
		);
	} finally {
		globalThis.fetch = originalFetch;
	}
});

test('fetchAnlzForDeckLoad does not block the load out waiting on a saturated decoder, and does not double-fire the ambient retry', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(
			anlzPayload({
				status: 'not_decoded',
				reason: 'decoder saturated',
				preview_b64: null,
				preview_max: null,
				retryable: true
			})
		);
	};
	// A deck load's own effect (WaveRow.svelte) registers the deck as a
	// consumer independently of this call - modelled here so the ambient
	// retry has somewhere to check (issue #735 follow-up, discussion_r3908644098).
	const token = cache.registerAnlzConsumer('deck-direct-still-saturated');
	try {
		const result = await cache.fetchAnlzForDeckLoad('deck-direct-still-saturated');
		assert.equal(calls, 1, 'the load must not wait out a cooldown before publishing');
		assert.equal(result.local_waveform.status, 'not_decoded');
		assert.equal(
			cache.deckAnlzNeedsFetch(result, null),
			true,
			'a still-retryable published result must keep reading as needing a fetch'
		);

		// _publishAnlzResult's own ambient self-schedule (not a second retry
		// hand-rolled here) is what picks this back up - confirm it fires
		// exactly once and does not double-fire from two independent
		// mechanisms racing the same cooldown (Codex finding, issue #735
		// follow-up, discussion_r3907928251 fix review).
		await new Promise((resolve) => setTimeout(resolve, RETRY_FIRES_WAIT_MS));
		assert.equal(calls, 2, 'the ambient self-schedule must fire exactly one retry, not two');
	} finally {
		globalThis.fetch = originalFetch;
		cache.unregisterAnlzConsumer('deck-direct-still-saturated', token);
	}
});

test('the ambient retry drops itself once its stable_id has no registered consumer left', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(
			anlzPayload({
				status: 'not_decoded',
				reason: 'decoder saturated',
				preview_b64: null,
				preview_max: null,
				retryable: true
			})
		);
	};
	try {
		// A row is selected (registers a consumer), the fetch comes back
		// retryable and schedules its ambient retry, then the row is
		// deselected (unregisters) before the cooldown elapses - exactly the
		// rapid-navigation scenario from discussion_r3908644098.
		const token = cache.registerAnlzConsumer('abandoned-track');
		cache.ensureAnlz('abandoned-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 1);
		cache.unregisterAnlzConsumer('abandoned-track', token);

		await new Promise((resolve) => setTimeout(resolve, RETRY_FIRES_WAIT_MS));
		assert.equal(
			calls,
			1,
			'nothing consumes this stable_id any more - the ambient retry must not keep firing'
		);

		// The entry must not be lost or poisoned by the dropped retry: it
		// stays the retryable class and due for refetch, so a consumer
		// returning later revives it with no special-casing.
		const entry = cache.getAnlzEntry('abandoned-track');
		assert.equal(entry.status, 'ready');
		assert.equal(cache.isAnlzEntryUsable(entry), false, 'still the retryable class, not a cache hit');

		cache.ensureAnlz('abandoned-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 2, 'a consumer returning later must revive the retry, not stay dropped forever');
	} finally {
		globalThis.fetch = originalFetch;
	}
});

test('the ambient retry keeps firing across multiple cooldowns while a consumer stays registered', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(
			anlzPayload({
				status: 'not_decoded',
				reason: 'decoder saturated',
				preview_b64: null,
				preview_max: null,
				retryable: true
			})
		);
	};
	const token = cache.registerAnlzConsumer('still-watched-track');
	try {
		cache.ensureAnlz('still-watched-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 1);

		await new Promise((resolve) => setTimeout(resolve, RETRY_FIRES_WAIT_MS));
		assert.equal(calls, 2, 'a live consumer must keep the ambient retry firing');
	} finally {
		globalThis.fetch = originalFetch;
		cache.unregisterAnlzConsumer('still-watched-track', token);
	}
});

test('unregisterAnlzConsumer is a safe no-op for an unknown or already-released token', () => {
	const token = cache.registerAnlzConsumer('solo-track');
	cache.unregisterAnlzConsumer('solo-track', token);
	assert.doesNotThrow(() => cache.unregisterAnlzConsumer('solo-track', token));
	assert.doesNotThrow(() => cache.unregisterAnlzConsumer('never-registered-track', Symbol()));
});

test('deckAnlzNeedsFetch: a still-retryable deck.anlz keeps needing a fetch, not just a null one', () => {
	const decoded = anlzPayload({ status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 });
	const retryable = anlzPayload({
		status: 'not_decoded',
		reason: 'decoder saturated',
		preview_b64: null,
		preview_max: null,
		retryable: true
	});
	const permanent = anlzPayload({
		status: 'not_decoded',
		reason: 'ffmpeg is not installed',
		preview_b64: null,
		preview_max: null
	});

	// No anlz yet: needs a fetch, same as before this fix.
	assert.equal(cache.deckAnlzNeedsFetch(null, null), true);
	// An anlz_error is terminal - never refetch behind it.
	assert.equal(cache.deckAnlzNeedsFetch(null, 'FETCH_FAILED'), false);
	// A terminal anlz (decoded or permanently not_decoded) is done.
	assert.equal(cache.deckAnlzNeedsFetch(decoded, null), false);
	assert.equal(cache.deckAnlzNeedsFetch(permanent, null), false);
	// A deck load's own fetchAnlzForDeckLoad can publish a retryable payload
	// straight away without blocking the load out on a saturated decoder
	// (Codex finding, issue #735 follow-up, discussion_r3907741366) - that
	// must keep reading as "needs a fetch", or WaveRow's effect would never
	// call ensureAnlz again and the deck would stay blank forever.
	assert.equal(cache.deckAnlzNeedsFetch(retryable, null), true);
});

test('a retryable cache write self-schedules its own retry, with no caller involved beyond keeping a consumer registered', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		if (calls < 3) {
			return jsonResponse(
				anlzPayload({
					status: 'not_decoded',
					reason: 'decoder saturated',
					preview_b64: null,
					preview_max: null,
					retryable: true
				})
			);
		}
		return jsonResponse(
			anlzPayload({ status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 })
		);
	};
	// Models a row that stays selected (or a deck that stays loaded) for the
	// whole exchange - the self-scheduling under test here is "does the timer
	// keep firing on its own", not "does it survive with zero consumers",
	// which the dedicated drop test above covers (issue #735 follow-up,
	// discussion_r3908644098).
	const token = cache.registerAnlzConsumer('self-scheduling-track');
	try {
		cache.ensureAnlz('self-scheduling-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 1, 'the first fetch fires immediately');

		// Nobody calls ensureAnlz (or anything else) again from here - only
		// real time passing should trigger the next attempt, proving the
		// retry is SELF-scheduled rather than dependent on a Svelte $effect
		// rerunning, which does not happen on a bare timer with no reactive
		// dependency change (Codex finding, issue #735 follow-up,
		// discussion_r3907928251).
		await new Promise((resolve) => setTimeout(resolve, RETRY_FIRES_WAIT_MS));
		assert.equal(calls, 2, 'the cooldown elapsing on its own must trigger a second attempt');

		await new Promise((resolve) => setTimeout(resolve, RETRY_FIRES_WAIT_MS));
		assert.equal(calls, 3, 'still-retryable results keep self-scheduling until a terminal answer lands');

		const entry = cache.getAnlzEntry('self-scheduling-track');
		assert.equal(entry.data.local_waveform.status, 'decoded');

		// A terminal result must stop the chain - no further calls.
		await new Promise((resolve) => setTimeout(resolve, RETRY_FIRES_WAIT_MS));
		assert.equal(calls, 3, 'a terminal result must not schedule another retry');
	} finally {
		globalThis.fetch = originalFetch;
		cache.unregisterAnlzConsumer('self-scheduling-track', token);
	}
});

test('resolveDisplayedAnlz: a still-retryable deck.anlz defers to a fresher cache entry', async () => {
	const retryable = anlzPayload({
		status: 'not_decoded',
		reason: 'decoder saturated',
		preview_b64: null,
		preview_max: null,
		retryable: true
	});
	const decoded = anlzPayload({ status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 });

	// No deck.anlz, no cache entry: null, same as before this fix.
	assert.equal(cache.resolveDisplayedAnlz(null, null), null);
	// A terminal deck.anlz always wins - unchanged fast path for every
	// normal (non-retryable) load.
	assert.equal(cache.resolveDisplayedAnlz(decoded, 'irrelevant-track'), decoded);

	// A retryable deck.anlz with nothing fresher cached: still shows the
	// retryable payload (matches today's rendering - a "not decoded yet"
	// row), not null.
	assert.equal(cache.resolveDisplayedAnlz(retryable, 'no-cache-track'), retryable);

	// The deck published a retryable anlz, but ensureAnlz's ambient retry
	// (driven by deckAnlzNeedsFetch) has since landed a real decode in the
	// shared cache. Without preferring the cache here, the row would keep
	// re-fetching forever (deckAnlzNeedsFetch) yet never actually SHOW the
	// decode, because deck.anlz itself is only written by a full load()
	// (Codex finding, issue #735 follow-up, discussion_r3907741366 - the
	// display half of the same gap).
	globalThis.fetch = async () =>
		jsonResponse(anlzPayload({ status: 'decoded', reason: null, preview_b64: 'BBBB', preview_max: 200 }));
	try {
		cache.ensureAnlz('landed-decode-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
	} finally {
		globalThis.fetch = originalFetch;
	}
	const resolved = cache.resolveDisplayedAnlz(retryable, 'landed-decode-track');
	assert.equal(resolved.local_waveform.status, 'decoded');
});

test('resolveDisplayedAnlz: a still-retryable deck.anlz that already carries an engine-merged beatgrid keeps that grid when a fresher cache entry lands', async () => {
	// PARITY-09's deferred upgrade (audio-engine.svelte.ts) merges the
	// analysis-derived grid into deck.anlz without touching local_waveform,
	// so a track whose waveform decode is still saturated/retryable stays
	// the retryable CLASS even after its grid has landed. The cache's own
	// /anlz refetch never carries that client-side merge - only deck.anlz
	// does - so preferring the cache entry wholesale here would silently
	// drop the already-merged grid the moment the waveform decode refreshes.
	const realBeats = [
		{ bpm: 120, n: 1, t: 0 },
		{ bpm: 120, n: 2, t: 0.5 },
		{ bpm: 120, n: 3, t: 1.0 },
		{ bpm: 120, n: 4, t: 1.5 }
	];
	const mergedButRetryable = {
		...anlzPayload({
			status: 'not_decoded',
			reason: 'decoder saturated',
			preview_b64: null,
			preview_max: null,
			retryable: true
		}),
		beatgrid: { beat_count: realBeats.length, beats: realBeats }
	};

	globalThis.fetch = async () =>
		jsonResponse(anlzPayload({ status: 'decoded', reason: null, preview_b64: 'CCCC', preview_max: 200 }));
	try {
		cache.ensureAnlz('merged-grid-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
	} finally {
		globalThis.fetch = originalFetch;
	}

	const resolved = cache.resolveDisplayedAnlz(mergedButRetryable, 'merged-grid-track');
	assert.equal(resolved.local_waveform.status, 'decoded', "the cache's refreshed waveform must still be adopted");
	assert.deepEqual(
		resolved.beatgrid,
		mergedButRetryable.beatgrid,
		'the engine-merged beatgrid must survive - the cache entry never has it, since only the engine merges the fallback grid'
	);
});

test('resolveDisplayedAnlz: a fresher cache entry that already carries a real beatgrid wins over a stale fallback grid on the deck', async () => {
	// discussion_r3916394792 (P2 BLOCKING): a local deck can be showing a
	// fallback (analysis-derived) beatgrid on deck.anlz while its waveform is
	// still retryable. If a rekordbox mapping lands and the cache's own
	// ambient /anlz retry races ahead of the engine's separate PARITY-09
	// upgrade fetch, the cache entry can carry the newly authoritative real
	// grid before deck.anlz does. That must not be discarded in favor of the
	// deck's older fallback grid just because the deck's grid is also "real".
	const fallbackBeats = [
		{ bpm: 128, n: 1, t: 0 },
		{ bpm: 128, n: 2, t: 0.46875 },
		{ bpm: 128, n: 3, t: 0.9375 },
		{ bpm: 128, n: 4, t: 1.40625 }
	];
	const authoritativeBeats = [
		{ bpm: 174, n: 1, t: 0 },
		{ bpm: 174, n: 2, t: 0.3448 },
		{ bpm: 174, n: 3, t: 0.6897 },
		{ bpm: 174, n: 4, t: 1.0345 }
	];
	const staleDeckAnlz = {
		...anlzPayload({
			status: 'not_decoded',
			reason: 'decoder saturated',
			preview_b64: null,
			preview_max: null,
			retryable: true
		}),
		beatgrid: { beat_count: fallbackBeats.length, beats: fallbackBeats }
	};

	globalThis.fetch = async () =>
		jsonResponse({
			...anlzPayload({ status: 'decoded', reason: null, preview_b64: 'DDDD', preview_max: 200 }),
			beatgrid: { beat_count: authoritativeBeats.length, beats: authoritativeBeats }
		});
	try {
		cache.ensureAnlz('mapping-landed-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
	} finally {
		globalThis.fetch = originalFetch;
	}

	const resolved = cache.resolveDisplayedAnlz(staleDeckAnlz, 'mapping-landed-track');
	assert.deepEqual(
		resolved.beatgrid,
		{ beat_count: authoritativeBeats.length, beats: authoritativeBeats },
		'the fresher cache-entry beatgrid must win once it is itself real, not the older fallback grid the deck still holds'
	);
});

test('refreshAnlzCacheEntry overwrites a stale published entry without fetching', async () => {
	globalThis.fetch = async () => jsonResponse(anlzPayload({ status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 }));
	try {
		// Seed the shared cache the way a normal deck load does, standing in
		// for the pre-mutation state a hot cue save leaves behind: the bank
		// (always a live fetch) and the cached anlz agree here, before the
		// mutation this test goes on to model.
		cache.ensureAnlz('mutated-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		const stale = cache.getAnlzEntry('mutated-track');
		assert.equal(stale.status, 'ready');
		assert.deepEqual(stale.data.cues, [], 'the seeded entry has no cues yet');

		// refreshHotCues (audio-engine) fetches this fresh payload via its own
		// fetchAnlz call, bypassing ensureAnlz/fetchAnlzForDeckLoad entirely -
		// so nothing but an explicit refreshAnlzCacheEntry call can be the
		// thing that lands it in the shared cache. No fetch mock swap here:
		// this call must not perform a fetch of its own.
		const freshCue = { slot: 'A', kind: 'hot_cue', is_loop: false, in_ms: 1000, out_ms: null };
		cache.refreshAnlzCacheEntry('mutated-track', anlzPayload({
			status: 'decoded',
			reason: null,
			preview_b64: 'AAAA',
			preview_max: 200
		}, [freshCue]));

		const refreshed = cache.getAnlzEntry('mutated-track');
		assert.equal(cache.isAnlzEntryUsable(refreshed), true);
		assert.deepEqual(
			refreshed.data.cues,
			[freshCue],
			'a later load() reusing this cache entry (isAnlzEntryUsable) must see the ' +
				'post-mutation cue, matching what the hot cue bank already shows - ' +
				'otherwise the bank and the waveform disagree after a reload, the ' +
				'issue #877 failure shape reached via a mutate-then-reload path ' +
				'(discussion_r3917488672)'
		);
	} finally {
		globalThis.fetch = originalFetch;
	}
});

test('invalidateAnlzCacheEntry evicts a ready entry so a later ensureAnlz refetches it', async () => {
	globalThis.fetch = async () => jsonResponse(anlzPayload({ status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 }));
	try {
		// Seed a ready entry, standing in for the pre-mutation cache state
		// refreshHotCues finds itself unable to overwrite when one of its two
		// post-write GETs fails (discussion_r3918817422): with no invalidation,
		// this stale entry stays 'ready' and isAnlzEntryUsable forever, so a
		// later load() of the track reuses it instead of refetching.
		cache.ensureAnlz('partially-refreshed-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(cache.getAnlzEntry('partially-refreshed-track').status, 'ready');

		cache.invalidateAnlzCacheEntry('partially-refreshed-track');
		assert.equal(
			cache.getAnlzEntry('partially-refreshed-track'),
			undefined,
			'an invalidated entry must read back as never-requested, not as a stale ready one'
		);

		let calls = 0;
		globalThis.fetch = async () => {
			calls += 1;
			return jsonResponse(
				anlzPayload({ status: 'decoded', reason: null, preview_b64: 'BBBB', preview_max: 200 })
			);
		};
		cache.ensureAnlz('partially-refreshed-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 1, 'ensureAnlz must treat an invalidated entry as a cache miss');
	} finally {
		globalThis.fetch = originalFetch;
	}
});

test('a decoded payload is terminal and never refetches', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(
			anlzPayload({
				status: 'decoded',
				reason: null,
				preview_b64: 'AAAA',
				preview_max: 200
			})
		);
	};
	try {
		cache.ensureAnlz('decoded-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 1);

		cache.ensureAnlz('decoded-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(calls, 1);
	} finally {
		globalThis.fetch = originalFetch;
	}
});

// ---------------------------------------- stale-source prefetch eviction

// evictAnlzCacheEntriesServingOtherSource's regression tests moved to
// analysis-source-deck-refresh.test.mjs (discussion_r3974993960 P1 BLOCKING):
// a fabricated globalThis.fetch response here let the assertions pass without
// exercising the production request, source-selection, parser, or error
// paths. They now run against that file's real fixture server, the same move
// discussion_r3970117748 already made for invalidateAllAnlzCacheEntries.
