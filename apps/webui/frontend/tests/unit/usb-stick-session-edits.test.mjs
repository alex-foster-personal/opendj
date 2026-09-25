import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Play from USB, lane D (specs/usb-play-from-stick.md 4b "Frontend guards":
 * "Hot cue writes on a stick deck stay in the session and make no request",
 * and decision 2: edits on a stick track are session only).
 *
 * The api-rb hot cue functions keep the library routes' contract for a stick
 * id (slot revisions, single-use reversals) but answer from an in-page session
 * store, so the pads, the IPC and MIDI keep calling the same functions. The
 * stick's own slots and /anlz are read over the stick routes (GET only), and
 * every read carries the session's edits.
 *
 * Every stick case pairs with a LIBRARY control on the same call, so a guard
 * that swallowed every id, or one that let the stick reach a write route,
 * fails here. The stick routes are answered by `stickRoutes` below from
 * SYNTHETIC data (no file or title from a real stick).
 *
 * Regression lines:
 * - if a hot cue save, clear or undo on a stick deck sends any non-GET request then the stick contract (never written) is broken
 * - if a stick hot cue edit is not visible on the next /hot-cues or /anlz read then the pads and the waveform disagree
 * - if a stale revision or a spent, foreign or overtaken reversal is accepted then the session store has lost the CAS contract the library has
 * - if hot_cue_save refuses a stick deck for having no rekordbox mapping then the DJ cannot set a cue on a stick track
 * - if hot_cue_save stops refusing an unmapped LIBRARY deck then a djmdCue write with nowhere to land 404s (#736)
 * - if the deck-header rating on a stick deck makes a request or toasts an error then decision 2 (session only) is broken
 * - if a cached stick /anlz entry is evicted by the own-lane poll then every poll bumps the fetch generation and discards unrelated prefetches
 * - if the resync guard rejects a stick grid while the lane is own then a stick deck whose grid arrives late never gets it
 */

const API_BASE = 'https://usb-stick-session-edits.example.test';
// Synthetic VolumeUUID, not the test stick's.
const STICK_VOLUME = '0F1E2D3C-4B5A-4978-8796-A5B4C3D2E1F0';
let stickSequence = 0;
/** A fresh stick id per test: the session store lives for the page, which
 * here is the whole test file. */
function freshStickId() {
	stickSequence += 1;
	return `usb-${STICK_VOLUME}-${stickSequence}`;
}
// The exact construction of apps/shared/state/ids.py::stable_id (every tier).
const LIBRARY_ID = createHash('sha1').update('usb-stick-session-edits library control').digest('hex');
const SLOTS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

let m;
let requests;
const originalFetch = globalThis.fetch;

//-----------------------------------------------------------------------------
// _helpers
//-----------------------------------------------------------------------------

function stickHotCue(slot, in_ms, color_table_index, comment) {
	return {
		kind: 'hot_cue',
		slot,
		in_ms,
		out_ms: null,
		is_loop: false,
		active_loop: false,
		beat_loop_size: null,
		color_table_index,
		comment
	};
}

const MEMORY_CUE = { ...stickHotCue(null, 500, null, null), kind: 'memory' };
const STICK_CUE_A = stickHotCue('A', 1000, 5, 'drop');

function stickSlots(id) {
	return SLOTS.map((slot) => ({
		slot,
		cue: slot === 'A' ? STICK_CUE_A : null,
		revision: `stick-${id}-${slot}`
	}));
}

function stickAnlz(id) {
	const bands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id: id,
		points: 1200,
		waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
		beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] },
		beatgrid_source: 'rekordbox',
		beatgrid_own_unavailable_reason: null,
		cues: [MEMORY_CUE, STICK_CUE_A],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
}

function json(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

/** Answers the read-only stick routes from synthetic data; everything else
 * gets a typed 503, so a library control only proves "a request was made". */
function stickRoutes() {
	return async (input, init = {}) => {
		const url = input instanceof Request ? input.url : String(input);
		const method = (init.method ?? 'GET').toUpperCase();
		const headers = new Headers(init.headers ?? {});
		requests.push({ method, url, ifMatch: headers.get('if-match') });
		const stick = /\/api\/v1\/usb\/tracks\/([^/?]+)(\/[^?]*)?/.exec(url);
		if (method === 'GET' && stick !== null) {
			const id = decodeURIComponent(stick[1]);
			if (stick[2] === '/hot-cues') return json(stickSlots(id));
			if (stick[2] === '/anlz') return json(stickAnlz(id));
			if (stick[2] === undefined) return json({ stable_id: id, title: 'synthetic', rating: 2, has_rb_mapping: false });
		}
		return json({ detail: { code: 'TEST_DAEMON_UNREACHABLE', message: 'no daemon in this unit test' } }, 503);
	};
}

function writeRequests() {
	return requests.filter(({ method }) => method !== 'GET');
}

function libraryRouteRequests() {
	return requests.filter(({ url }) => url.includes('/api/v1/tracks/'));
}

function slotOf(slots, slot) {
	return slots.find((entry) => entry.slot === slot);
}

async function refusal(promise, code) {
	await assert.rejects(promise, (error) => {
		assert.equal(error.code, code, error.message);
		assert.equal(error.status, 0, 'status 0 says no HTTP exchange happened');
		return true;
	});
}

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/stick-session-edits-entry.ts', { viteApiBase: API_BASE });
});

beforeEach(() => {
	requests = [];
	globalThis.fetch = stickRoutes();
});

after(() => {
	globalThis.fetch = originalFetch;
});

//-----------------------------------------------------------------------------
// api-rb hot cue functions on a stick id
//-----------------------------------------------------------------------------

test('[if] a stick hot cue is saved, cleared and undone [then] each stays in the session with no write request, and every read shows it', async () => {
	const id = freshStickId();
	const own = await m.fetchHotCueSlots(id);
	assert.deepEqual(own, stickSlots(id), 'no edit yet: the stick slots pass through untouched');
	assert.deepEqual(requests.map(({ url }) => url), [`${API_BASE}/api/v1/usb/tracks/${id}/hot-cues`]);

	const saved = await m.saveHotCue(id, 'B', 2000, slotOf(own, 'B').revision, 'build');
	assert.deepEqual(saved.cue, { ...stickHotCue('B', 2000, null, 'build') });
	assert.match(saved.revision, /^session-revision-\d+$/);
	assert.match(saved.reversal.reversal_id, /^session-reversal-\d+$/);

	const afterSave = await m.fetchHotCueSlots(id);
	assert.deepEqual(slotOf(afterSave, 'B'), { slot: 'B', cue: saved.cue, revision: saved.revision });
	assert.deepEqual(slotOf(afterSave, 'A'), slotOf(own, 'A'), 'an unedited slot keeps the stick cue and revision');
	assert.deepEqual((await m.fetchAnlz(id)).cues, [MEMORY_CUE, STICK_CUE_A, saved.cue], '/anlz carries the session cue too');

	const cleared = await m.clearHotCue(id, 'A', slotOf(own, 'A').revision);
	assert.equal(cleared.cue, null);
	assert.equal(slotOf(await m.fetchHotCueSlots(id), 'A').cue, null);
	assert.deepEqual((await m.fetchAnlz(id)).cues, [MEMORY_CUE, saved.cue], 'the cleared stick cue is gone, the memory cue stays');

	const restored = await m.restoreHotCue(id, 'A', cleared.revision, cleared.reversal.reversal_id);
	assert.deepEqual(restored.cue, STICK_CUE_A, 'undo brings the stick cue back with its color and comment');
	assert.notEqual(restored.revision, slotOf(own, 'A').revision, 'a revision is never reused');
	assert.deepEqual(slotOf(await m.fetchHotCueSlots(id), 'A'), { slot: 'A', cue: STICK_CUE_A, revision: restored.revision });

	assert.deepEqual(writeRequests(), [], 'no PUT/DELETE of any kind');
	assert.deepEqual(libraryRouteRequests(), [], 'no request to a library route');
	assert.ok(requests.every(({ url }) => url.startsWith(`${API_BASE}/api/v1/usb/tracks/${id}/`)), JSON.stringify(requests));
});

test('[if] a stick edit presents a stale revision or a spent, foreign or overtaken reversal [then] it is refused like the library route would refuse it', async () => {
	const id = freshStickId();
	const own = await m.fetchHotCueSlots(id);
	const first = await m.saveHotCue(id, 'B', 2000, slotOf(own, 'B').revision);

	await refusal(m.saveHotCue(id, 'B', 3000, slotOf(own, 'B').revision), 'HOT_CUE_REVISION_CONFLICT');
	await refusal(m.restoreHotCue(id, 'C', slotOf(own, 'C').revision, first.reversal.reversal_id), 'HOT_CUE_REVERSAL_SCOPE_CONFLICT');
	await refusal(m.restoreHotCue(freshStickId(), 'B', first.revision, first.reversal.reversal_id), 'HOT_CUE_REVERSAL_SCOPE_CONFLICT');

	const second = await m.saveHotCue(id, 'B', 3000, first.revision);
	await refusal(m.restoreHotCue(id, 'B', second.revision, first.reversal.reversal_id), 'HOT_CUE_REVERSAL_STALE');

	await m.restoreHotCue(id, 'B', second.revision, second.reversal.reversal_id);
	const current = slotOf(await m.fetchHotCueSlots(id), 'B');
	assert.equal(current.cue.in_ms, 2000, 'undo of the second save returns the first');
	await refusal(m.restoreHotCue(id, 'B', current.revision, second.reversal.reversal_id), 'HOT_CUE_REVERSAL_NOT_FOUND');

	await refusal(m.saveHotCue(id, 'C', -1, slotOf(own, 'C').revision), 'HOT_CUE_POSITION_INVALID');
	await refusal(m.saveHotCue(id, 'C', 12.5, slotOf(own, 'C').revision), 'HOT_CUE_POSITION_INVALID');

	const unread = freshStickId();
	await assert.rejects(m.saveHotCue(unread, 'A', 0, 'anything'), /were never read/);
	assert.deepEqual(writeRequests(), []);
});

test('[if] the same hot cue calls get a library id [then] they still write to the library routes with If-Match', async () => {
	await assert.rejects(m.saveHotCue(LIBRARY_ID, 'A', 1000, 'etag-a'));
	await assert.rejects(m.clearHotCue(LIBRARY_ID, 'B', 'etag-b'));
	await assert.rejects(m.restoreHotCue(LIBRARY_ID, 'C', 'etag-c', 'reversal'));
	assert.deepEqual(writeRequests(), [
		{ method: 'PUT', url: `${API_BASE}/api/v1/tracks/${LIBRARY_ID}/hot-cues/A`, ifMatch: 'etag-a' },
		{ method: 'DELETE', url: `${API_BASE}/api/v1/tracks/${LIBRARY_ID}/hot-cues/B`, ifMatch: 'etag-b' },
		{ method: 'PUT', url: `${API_BASE}/api/v1/tracks/${LIBRARY_ID}/hot-cues/C/restore`, ifMatch: 'etag-c' }
	]);
});

//-----------------------------------------------------------------------------
// the shared edit gate, and the IPC (agent and MIDI entry point) end to end
//-----------------------------------------------------------------------------

test('[if] hotCueEditsAllowed is asked about each deck kind [then] only a mapped library deck or a stick deck takes edits', () => {
	const stick = freshStickId();
	assert.equal(m.hotCueEditsAllowed(null, true), false, 'empty deck (#804)');
	assert.equal(m.hotCueEditsAllowed(LIBRARY_ID, false), false, 'unmapped library track (#736)');
	assert.equal(m.hotCueEditsAllowed(LIBRARY_ID, true), true, 'mapped library track');
	assert.equal(m.hotCueEditsAllowed(stick, false), true, 'stick track: session edits need no mapping');
});

test('[if] hot_cue_save, clear and restore are dispatched for an unmapped stick deck [then] they succeed in the session with no write, while an unmapped library deck is still refused', async () => {
	let loaded = freshStickId();
	let mapped = false;
	let refreshed = 0;
	globalThis.window = {};
	const resetDriver = m.ipc.installPerformanceHotCueDriverForTest({
		stableId: () => loaded,
		refresh: async () => {
			refreshed += 1;
		},
		hasRbMapping: () => mapped
	});
	const uninstall = m.ipc.installPerformanceBrowserIpc();
	const dispatch = (command) => window.musicDjToolsPerformance.dispatch(command);
	try {
		const own = await m.fetchHotCueSlots(loaded);
		let state = await dispatch({ type: 'hot_cue_save', deck: 1, slot: 'B', in_ms: 3000, revision: slotOf(own, 'B').revision });
		assert.equal(state.decks[1].hot_cue_reversal.slot, 'B');
		assert.equal(slotOf(await m.fetchHotCueSlots(loaded), 'B').cue.in_ms, 3000);

		state = await dispatch({ type: 'hot_cue_clear', deck: 1, slot: 'A', revision: slotOf(own, 'A').revision });
		const reversal = state.decks[1].hot_cue_reversal;
		assert.equal(reversal.slot, 'A');
		state = await dispatch({ type: 'hot_cue_restore', deck: 1, slot: 'A', revision: reversal.revision, reversal_id: reversal.reversal_id });
		assert.equal(state.decks[1].hot_cue_reversal, null);
		assert.deepEqual(slotOf(await m.fetchHotCueSlots(loaded), 'A').cue, STICK_CUE_A);
		assert.equal(refreshed, 3, 'the deck re-reads after every edit, as for a library track');
		assert.deepEqual(writeRequests(), []);
		assert.deepEqual(libraryRouteRequests(), []);

		// Control: the mapping gate still bites for an unmapped LIBRARY deck.
		loaded = LIBRARY_ID;
		await assert.rejects(
			dispatch({ type: 'hot_cue_save', deck: 1, slot: 'A', in_ms: 1000, revision: 'etag' }),
			/cues need a rekordbox mapping/
		);
		assert.deepEqual(writeRequests(), [], 'refused before any request');
		mapped = true;
		await assert.rejects(dispatch({ type: 'hot_cue_save', deck: 1, slot: 'A', in_ms: 1000, revision: 'etag' }));
		assert.deepEqual(
			writeRequests().map(({ method, url }) => `${method} ${url}`),
			[`PUT ${API_BASE}/api/v1/tracks/${LIBRARY_ID}/hot-cues/A`],
			'control: a mapped library save still writes'
		);
	} finally {
		uninstall();
		resetDriver();
		delete globalThis.window;
	}
});

//-----------------------------------------------------------------------------
// deck-header rating (decision 2)
//-----------------------------------------------------------------------------

test('[if] the deck-header stars rate a stick track [then] the rating is a session edit with no request, and getTrack carries it', async () => {
	const id = freshStickId();
	const deck = m.deckStates[1];
	const before = { stable_id: deck.stable_id, rating: deck.rating };
	try {
		deck.stable_id = id;
		deck.rating = 2;
		const toastCount = m.toasts.length;
		await m.rateDeckTrack(1, 4);
		assert.equal(deck.rating, 4);
		assert.deepEqual(requests, [], 'no GET, no PATCH');
		assert.equal(m.toasts.length, toastCount, 'no error toast');
		assert.equal((await m.getTrack(id)).track.rating, 4, 'the stick said 2; the session rating wins for this page');

		await m.rateDeckTrack(1, 7);
		assert.equal(deck.rating, 4, 'an out-of-range rating is refused');
		assert.match(m.toasts.at(-1).message, /rating update failed: .*RATING_INVALID/);

		// Control: a library deck still asks the library route.
		requests = [];
		deck.stable_id = LIBRARY_ID;
		await m.rateDeckTrack(1, 3);
		assert.deepEqual(requests.map(({ method, url }) => `${method} ${url}`), [`GET ${API_BASE}/api/v1/tracks/${LIBRARY_ID}`]);
	} finally {
		deck.stable_id = before.stable_id;
		deck.rating = before.rating;
	}
});

//-----------------------------------------------------------------------------
// the analysis-source lane never evicts or rejects a stick grid
//-----------------------------------------------------------------------------

function gridlessAnlz(stable_id, beatgrid_source) {
	return { ...stickAnlz(stable_id), cues: [], beatgrid_source };
}

test('[if] the lane is own and the cache holds a stick entry and a library rekordbox entry [then] eviction drops only the library one, and a stick-only cache reports nothing evicted', async () => {
	const cache = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache.svelte.ts', { viteApiBase: API_BASE });
	const stick = freshStickId();
	cache.refreshAnlzCacheEntry(stick, gridlessAnlz(stick, 'rekordbox'));
	cache.refreshAnlzCacheEntry(LIBRARY_ID, gridlessAnlz(LIBRARY_ID, 'rekordbox'));
	assert.equal(cache.getAnlzEntry(stick)?.status, 'ready');
	assert.equal(cache.getAnlzEntry(LIBRARY_ID)?.status, 'ready');

	assert.equal(cache.evictAnlzCacheEntriesServingOtherSource('own'), true);
	assert.equal(cache.getAnlzEntry(LIBRARY_ID), undefined, 'control: the library entry serving the other source is evicted');
	assert.equal(cache.getAnlzEntry(stick)?.status, 'ready', 'the stick entry survives');
	assert.equal(
		cache.evictAnlzCacheEntriesServingOtherSource('own'),
		false,
		'false means analysis-source.svelte.ts does not bump the fetch generation on this poll'
	);
	assert.deepEqual(requests, []);
});

test('[if] an authoritative stick grid lands while the lane is own [then] the resync guard installs it, and still rejects a library grid stamped rekordbox', async () => {
	const { createBeatgridResyncGuards } = await loadTypeScriptModule('src/lib/player/beatgrid-resync-guards.ts');
	const stick = freshStickId();
	const beats = (count) => Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));
	const grid = (stable_id, count) => ({
		...gridlessAnlz(stable_id, 'rekordbox'),
		beatgrid: { source: 'rekordbox', beat_count: count, beats: beats(count) }
	});
	const stableIds = { 1: stick, 2: LIBRARY_ID, 3: null, 4: null };
	const anlz = { 1: grid(stick, 8), 2: grid(LIBRARY_ID, 8), 3: null, 4: null };
	// One stable runtime object per deck: a new object reads as a reload.
	const runtimes = { 1: { deck: 1 }, 2: { deck: 2 }, 3: { deck: 3 }, 4: { deck: 4 } };
	const noop = () => {};
	const guards = createBeatgridResyncGuards({
		ports: {
			deckIds: [1, 2, 3, 4],
			syncMaster: () => null,
			playing: () => false,
			beatSyncEnabled: () => false,
			setBeatSyncEnabled: noop,
			hasRealBeatGrid: () => true,
			hasSyncError: () => false,
			setSyncError: noop,
			requiresReschedule: () => false,
			synchronizeFollowers: () => Promise.resolve(),
			hasSettledGridless: () => false,
			markSettledGridless: noop,
			markPending: noop,
			takePending: () => [],
			hasPendingFollowers: () => false
		},
		deckRuntime: (deck) => runtimes[deck],
		deckLoadToken: () => 0,
		deckStableId: (deck) => stableIds[deck],
		deckAnlz: (deck) => anlz[deck],
		publishDeckAnlz: (deck, next) => (anlz[deck] = next),
		publishDeckBpm: noop,
		setDeckAnlzError: noop,
		reconcileDeckLoop: noop,
		reportError: (message) => assert.fail(message),
		desiredBeatgridSource: () => 'own'
	});
	guards.installScopedSyncRunner(async (_deck, task) => {
		await Promise.resolve();
		await task(async (work) => await work());
	});
	await Promise.all([
		guards.adoptAuthoritativeGrid(stick, grid(stick, 16)),
		guards.adoptAuthoritativeGrid(LIBRARY_ID, grid(LIBRARY_ID, 16))
	]);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(anlz[1].beatgrid.beats.length, 16, 'the stick grid is installed under the own lane');
	assert.equal(anlz[2].beatgrid.beats.length, 8, 'control: a library rekordbox grid under own is still rejected');
});

//-----------------------------------------------------------------------------
// HotCueBank (source-level: the repo has no component mount harness)
//-----------------------------------------------------------------------------

test('[if] HotCueBank renders a stick deck [then] its edit controls say the edit is session only', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/deck/HotCueBank.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(source, /const SESSION_TIP = ' \(this session only - the stick is never written\)';/);
	assert.match(source, /const onStick = \$derived\(deck\.stable_id !== null && isUsbTrackId\(deck\.stable_id\)\);/);
	assert.match(source, /const sessionTip = \$derived\(onStick \? SESSION_TIP : ''\);/);
	assert.match(source, /- click to save the current position\$\{sessionTip\}`/, 'empty pad');
	assert.match(source, /title=\{`edit hot cue \$\{entry\.slot\} label\$\{sessionTip\}`\}/, 'pencil');
	assert.match(source, /title=\{`clear hot cue \$\{entry\.slot\}\$\{sessionTip\}`\}/, 'clear');
});
