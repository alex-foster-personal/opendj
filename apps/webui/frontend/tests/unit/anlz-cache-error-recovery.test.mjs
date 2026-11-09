/**
 * Issue #1762 (PR #1587 discussion_r3979679828): a deck invalidated by an
 * RbApiError must hear a later successful, gridless rekordbox answer for the
 * same track, or its `anlz_error` stays terminal and `deckAnlzNeedsFetch`
 * stops it retrying.
 *
 * Its own file because the grid and error sinks are install-once per module
 * instance and anlz-cache.test.mjs already installs both; each node --test
 * file runs in a fresh process.
 */
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://anlz-cache-error-recovery.example.test';
const DECKS = [1, 2, 3, 4];
const ERROR_CODE = 'ANALYSIS_NOT_FOUND';

let cache;
let deckState;
const gridSinkCalls = [];
const originalFetch = globalThis.fetch;

function _ports() {
	return {
		deckIds: DECKS,
		syncMaster: () => null,
		playing: () => false,
		beatSyncEnabled: () => false,
		setBeatSyncEnabled: () => {},
		hasRealBeatGrid: () => true,
		hasSyncError: () => false,
		setSyncError: () => {},
		requiresReschedule: () => false,
		synchronizeFollowers: () => Promise.resolve(),
		hasSettledGridless: () => false,
		markSettledGridless: () => {},
		markPending: () => {},
		takePending: () => [],
		hasPendingFollowers: () => false
	};
}

before(async () => {
	cache = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache.svelte.ts', {
		viteApiBase: API_BASE
	});
	const stableIds = Object.fromEntries(DECKS.map((deck) => [deck, null]));
	const anlz = Object.fromEntries(DECKS.map((deck) => [deck, null]));
	const bpm = Object.fromEntries(DECKS.map((deck) => [deck, null]));
	const anlzError = Object.fromEntries(DECKS.map((deck) => [deck, null]));
	const runtimes = Object.fromEntries(DECKS.map((deck) => [deck, { deck }]));
	const errors = [];
	const guards = cache.createBeatgridResyncGuards({
		ports: _ports(),
		deckRuntime: (deck) => runtimes[deck],
		deckLoadToken: () => 0,
		deckStableId: (deck) => stableIds[deck],
		deckAnlz: (deck) => anlz[deck],
		publishDeckAnlz: (deck, next) => (anlz[deck] = next),
		publishDeckBpm: (deck, next) => (bpm[deck] = next),
		setDeckAnlzError: (deck, code) => (anlzError[deck] = code),
		reconcileDeckLoop: () => {},
		reportError: (message) => errors.push(message),
		desiredBeatgridSource: () => undefined
	});
	guards.installScopedSyncRunner(async (deck, task) => {
		await Promise.resolve();
		await task(async (work) => await work());
	});
	cache.installAuthoritativeAnlzGridSink((...args) => {
		gridSinkCalls.push(args[0]);
		return guards.adoptAuthoritativeGrid(...args);
	});
	cache.installAuthoritativeAnlzErrorSink(guards.adoptAuthoritativeError);
	deckState = { stableIds, anlz, anlzError, errors };
});

afterEach(() => {
	globalThis.fetch = originalFetch;
	for (const deck of DECKS) {
		deckState.stableIds[deck] = null;
		deckState.anlz[deck] = null;
		deckState.anlzError[deck] = null;
	}
	assert.deepEqual(deckState.errors, [], 'guards reported an error');
});

function gridlessRekordbox(stableId) {
	return {
		stable_id: stableId,
		points: 38400,
		waveform: {
			kind: 'mono',
			preview: { length: 0, low: [], mid: [], high: [] },
			detail: { length: 0, low: [], mid: [], high: [] }
		},
		beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		local_waveform: { status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 },
		vocals: { status: 'not_analyzed' }
	};
}

function answerGridless(stableId) {
	globalThis.fetch = async () =>
		new Response(JSON.stringify(gridlessRekordbox(stableId)), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
}

function answerApiError() {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: { code: ERROR_CODE, message: 'gone' } }), {
			status: 404,
			headers: { 'content-type': 'application/json' }
		});
}

async function waitFor(predicate, label) {
	const deadline = Date.now() + 2000;
	while (!predicate()) {
		if (Date.now() > deadline) assert.fail(`timed out waiting for: ${label}`);
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

async function flush() {
	for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

/** Mirrors what the engine's load() publishes for a deck. */
async function loadDeck(deck, stableId) {
	const data = await cache.fetchAnlzForDeckLoad(stableId);
	const entry = cache.getAnlzEntry(stableId);
	deckState.stableIds[deck] = stableId;
	deckState.anlz[deck] = data;
	deckState.anlzError[deck] = entry?.status === 'error' ? entry.code : null;
}

async function errorTrack(stableId, decks) {
	answerApiError();
	cache.revalidateAnlz(stableId);
	await waitFor(
		() => decks.every((deck) => deckState.anlzError[deck] === ERROR_CODE),
		'decks carry the error'
	);
	assert.equal(cache.getAnlzEntry(stableId).status, 'error');
}

test('after an error entry, a gridless rekordbox answer from a second deck load clears the first deck anlz_error', async () => {
	const id = 'recover-via-load';
	answerGridless(id);
	await loadDeck(1, id);
	await errorTrack(id, [1]);

	answerGridless(id);
	await loadDeck(2, id);
	await waitFor(() => deckState.anlzError[1] === null, 'deck 1 error cleared');
	assert.equal(deckState.anlzError[2], null);
	assert.equal(cache.getAnlzEntry(id).status, 'ready');
	assert.ok(gridSinkCalls.includes(id));
	assert.equal(deckState.anlz[1].beatgrid.source, 'rekordbox');
	assert.equal(deckState.anlz[2].beatgrid.source, 'rekordbox');
});

test('a gridless rekordbox revalidation clears anlz_error on every deck holding the track', async () => {
	const id = 'recover-via-revalidate';
	answerGridless(id);
	await loadDeck(1, id);
	await loadDeck(2, id);
	await errorTrack(id, [1, 2]);

	answerGridless(id);
	cache.revalidateAnlz(id);
	await waitFor(
		() => deckState.anlzError[1] === null && deckState.anlzError[2] === null,
		'both decks error cleared'
	);
	assert.equal(cache.getAnlzEntry(id).status, 'ready');
});

test('a real api error after an error entry leaves the deck in error', async () => {
	const id = 'stays-errored';
	answerGridless(id);
	await loadDeck(1, id);
	await errorTrack(id, [1]);
	const sinkCallsBefore = gridSinkCalls.length;

	answerApiError();
	cache.revalidateAnlz(id);
	await assert.rejects(cache.fetchAnlzForDeckLoad(id));
	await flush();
	assert.equal(deckState.anlzError[1], ERROR_CODE);
	assert.equal(cache.getAnlzEntry(id).status, 'error');
	assert.equal(gridSinkCalls.length, sinkCallsBefore);
});

test('a same-source gridless rekordbox retry with no prior error still does not reach the engine', async () => {
	const id = 'no-retry-storm';
	answerGridless(id);
	await loadDeck(1, id);
	const sinkCallsBefore = gridSinkCalls.filter((call) => call === id).length;

	cache.revalidateAnlz(id);
	await flush();
	assert.equal(gridSinkCalls.filter((call) => call === id).length, sinkCallsBefore);
});
