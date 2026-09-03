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
 * Regression lines:
 * - if a deck with a loaded track doesn't get a fresh, cache-bypassing /anlz fetch
 *   republished onto st.anlz then broken
 * - if an unloaded deck (stable_id null) triggers a fetch anyway then broken
 * - if a deck swapped to a different track mid-request has its stale response
 *   written onto the new track's anlz then broken
 */
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://analysis-source-deck-refresh.example.test';

let audio;
let originalFetch;

function anlzPayload(beatCount) {
	return {
		stable_id: 'irrelevant-server-echoes-request-path',
		points: 38400,
		waveform: {
			kind: 'mono',
			preview: { length: 0, low: [], mid: [], high: [] },
			detail: { length: 0, low: [], mid: [], high: [] }
		},
		beatgrid: { beat_count: beatCount, beats: [] },
		cues: [],
		phrases: [],
		local_waveform: { status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 },
		vocals: { status: 'not_analyzed' }
	};
}

function jsonResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

before(async () => {
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

beforeEach(() => {
	for (const deck of audio.DECK_IDS) {
		audio.deckStates[deck].stable_id = null;
		audio.deckStates[deck].anlz = null;
	}
});

test('refreshes a loaded deck onto a fresh, cache-bypassing anlz fetch', async () => {
	const requestedUrls = [];
	audio.deckStates[1].stable_id = 'track-a';
	audio.deckStates[1].anlz = anlzPayload(4);

	globalThis.fetch = async (url, init) => {
		requestedUrls.push({ url, cache: init?.cache });
		return jsonResponse(anlzPayload(64));
	};
	try {
		await audio.engine.refreshDecksForAnalysisSourceChange();
	} finally {
		globalThis.fetch = originalFetch;
	}

	assert.equal(audio.deckStates[1].anlz.beatgrid.beat_count, 64, 'deck 1 must adopt the fresh payload');
	assert.equal(requestedUrls.length, 1, 'only the one loaded deck should fetch');
	assert.match(requestedUrls[0].url, /\/tracks\/track-a\/anlz\?points=/);
	assert.equal(requestedUrls[0].cache, 'reload', 'must bypass the /anlz 1h HTTP cache (_CACHE_ANLZ)');
});

test('an unloaded deck (no stable_id) triggers no fetch and is left untouched', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(anlzPayload(64));
	};
	try {
		await audio.engine.refreshDecksForAnalysisSourceChange();
	} finally {
		globalThis.fetch = originalFetch;
	}

	assert.equal(calls, 0, 'no deck has a stable_id, so nothing should be fetched');
	for (const deck of audio.DECK_IDS) {
		assert.equal(audio.deckStates[deck].anlz, null);
	}
});

test('refreshes every loaded deck independently', async () => {
	audio.deckStates[1].stable_id = 'track-a';
	audio.deckStates[2].stable_id = 'track-b';
	audio.deckStates[1].anlz = anlzPayload(1);
	audio.deckStates[2].anlz = anlzPayload(2);

	globalThis.fetch = async (url) => {
		const beats = url.includes('track-a') ? 10 : 20;
		return jsonResponse(anlzPayload(beats));
	};
	try {
		await audio.engine.refreshDecksForAnalysisSourceChange();
	} finally {
		globalThis.fetch = originalFetch;
	}

	assert.equal(audio.deckStates[1].anlz.beatgrid.beat_count, 10);
	assert.equal(audio.deckStates[2].anlz.beatgrid.beat_count, 20);
});

test('a deck swapped to a different track mid-request keeps the new track, not the stale response', async () => {
	audio.deckStates[1].stable_id = 'track-a';
	audio.deckStates[1].anlz = anlzPayload(1);

	let resolveFetch;
	globalThis.fetch = () =>
		new Promise((resolve) => {
			resolveFetch = () => resolve(jsonResponse(anlzPayload(999)));
		});

	const refreshPromise = audio.engine.refreshDecksForAnalysisSourceChange();
	// Simulate a real deck load landing before the analysis-source refetch settles.
	await new Promise((resolve) => setTimeout(resolve, 0));
	audio.deckStates[1].stable_id = 'track-c';
	const newAnlz = anlzPayload(7);
	audio.deckStates[1].anlz = newAnlz;

	try {
		resolveFetch();
		await refreshPromise;
	} finally {
		globalThis.fetch = originalFetch;
	}

	assert.equal(audio.deckStates[1].stable_id, 'track-c');
	assert.equal(
		audio.deckStates[1].anlz,
		newAnlz,
		'the stale track-a response must not overwrite the deck that has since loaded track-c'
	);
});
