import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Play from USB, lane D (specs/usb-play-from-stick.md 4b "Frontend guards"):
 * every per-track builder outside api-rb.ts/api.ts routes a stick id or makes
 * no request for it, hot cue writes on a stick deck make no request, the
 * analysis-source confirmation loop exempts stick ids, suggest-next leaves
 * them out, and a failed stick load says what happened in plain words.
 *
 * Every stick case pairs with a LIBRARY control on the same call: a guard that
 * blocked every id would pass a stick-only assertion, so each control proves
 * the guard is keyed on the id and the library path still reaches the network.
 * The library controls answer from `unreachableDaemon`, a typed 503 envelope,
 * so the assertion is only "a request was made to the library path".
 *
 * Regression lines:
 * - if a library-only builder (auto-cues, beatgrid fallback, playlists, reveal, remove, lyrics, stem waveform) reaches fetch for a stick id then the stick deck fires a 404 per mount
 * - if the auto-cue or stem waveform cache records FETCH_FAILED for a stick id then the deck shows a transport error instead of "not available"
 * - if the source-confirmation loop does not exempt a stick payload then a stick deck spins re-fetching /anlz forever while the lane is own
 * - if refreshAnalysisSourceDecks re-fetches a stick deck then the batch serves rekordbox beside own and every switch throws the split error
 * - if a gridless stick deck asks rb-meta or the beatgrid fallback then two library-only 404s fire per load
 * - if hot_cue_clear or hot_cue_restore reach the network for a stick deck then decision 2 (never written) is broken
 * - if a stick load failure keeps the generic headline then "Stick removed" never reaches the DJ
 * - if suggest-next sends a stick id then the copilot 404s and library suggestions fail too
 */

const API_BASE = 'https://usb-track-guards.example.test';
const STICK_ID = 'usb-C74521A7-BFC7-3382-B11A-FBE569767B61-36';
// The exact construction of apps/shared/state/ids.py::stable_id (every tier).
const LIBRARY_ID = createHash('sha1').update('usb-track-guards library control').digest('hex');

let requests;
const originalFetch = globalThis.fetch;

function urlOf(input) {
	return input instanceof Request ? input.url : String(input);
}

function refuseEveryRequest() {
	return async (input) => {
		requests.push(urlOf(input));
		throw new Error(`no request expected in this test, got ${urlOf(input)}`);
	};
}

function unreachableDaemon() {
	return async (input) => {
		requests.push(urlOf(input));
		return new Response(
			JSON.stringify({ detail: { code: 'TEST_DAEMON_UNREACHABLE', message: 'no daemon in this unit test' } }),
			{ status: 503, headers: { 'content-type': 'application/json' } }
		);
	};
}

function settle() {
	return new Promise((resolve) => setImmediate(resolve));
}

function gridlessAnlz(stable_id, beatgrid_source) {
	const bands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id,
		points: 1200,
		waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
		beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] },
		beatgrid_source,
		beatgrid_own_unavailable_reason: null,
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
}

beforeEach(() => {
	requests = [];
	globalThis.fetch = refuseEveryRequest();
});

after(() => {
	globalThis.fetch = originalFetch;
});

//-----------------------------------------------------------------------------
// library-only builders: a stick id is refused before any request
//-----------------------------------------------------------------------------

const LIBRARY_ONLY_BUILDERS = [
	{
		module: 'src/lib/rb/auto-cues-api.ts',
		name: 'fetchAutoCues',
		call: (m, id) => m.fetchAutoCues(id),
		code: 'USB_NOT_SUPPORTED',
		libraryPath: `/api/v1/tracks/${LIBRARY_ID}/auto-cues`
	},
	{
		module: 'src/lib/rb/beatgrid-fallback-api.ts',
		name: 'fetchBeatgridFallback',
		call: (m, id) => m.fetchBeatgridFallback(id),
		code: 'USB_NOT_SUPPORTED',
		libraryPath: `/api/v1/tracks/${LIBRARY_ID}/beatgrid-fallback`
	},
	{
		module: 'src/lib/rb/track-playlists.ts',
		name: 'listTrackPlaylists',
		call: (m, id) => m.listTrackPlaylists(id),
		code: 'USB_NOT_SUPPORTED',
		libraryPath: `/api/v1/tracks/${LIBRARY_ID}/playlists`
	},
	{
		module: 'src/lib/rb/track-library.ts',
		name: 'removeFromLibrary',
		call: (m, id) => m.removeFromLibrary(id),
		code: 'USB_READ_ONLY',
		libraryPath: `/api/v1/tracks/${LIBRARY_ID}:remove`
	},
	{
		module: 'src/lib/rb/api-track-reveal.ts',
		name: 'revealTrack',
		call: (m, id) => m.revealTrack(id),
		code: 'USB_NOT_SUPPORTED',
		libraryPath: `/api/v1/tracks/${LIBRARY_ID}:reveal`
	},
	{
		module: 'src/lib/api-karaoke.ts',
		name: 'putLyricOverride',
		call: (m, id) => m.putLyricOverride(id, null),
		code: 'USB_READ_ONLY',
		libraryPath: `/api/v1/tracks/${LIBRARY_ID}/lyrics/override`
	}
];

for (const builder of LIBRARY_ONLY_BUILDERS) {
	test(`[if] ${builder.name} is called for a stick id [then] it refuses ${builder.code} with no request, and a library id still reaches ${builder.libraryPath}`, async () => {
		const m = await loadTypeScriptModule(builder.module, { viteApiBase: API_BASE });
		await assert.rejects(builder.call(m, STICK_ID), (error) => {
			assert.equal(error.name, 'UsbTrackRefusal');
			assert.equal(error.code, builder.code);
			assert.equal(error.status, 0, 'status 0 says no HTTP exchange happened');
			return true;
		});
		assert.deepEqual(requests, [], `${builder.name} must not reach fetch for a stick id`);

		globalThis.fetch = unreachableDaemon();
		await builder.call(m, LIBRARY_ID).catch(() => {});
		assert.deepEqual(requests, [`${API_BASE}${builder.libraryPath}`], `${builder.name} control: the library path is untouched`);
	});
}

test('[if] karaoke words are asked for a stick id [then] the answer is the no-lyrics null with no request, and a library id still fetches', async () => {
	const karaoke = await loadTypeScriptModule('src/lib/api-karaoke.ts', { viteApiBase: API_BASE });
	assert.equal(await karaoke.getTrackLyricsWords(STICK_ID), null);
	assert.deepEqual(requests, []);

	globalThis.fetch = unreachableDaemon();
	await karaoke.getTrackLyricsWords(LIBRARY_ID).catch(() => {});
	assert.equal(requests.length, 1);
	assert.ok(requests[0].startsWith(`${API_BASE}/api/v1/tracks/${LIBRARY_ID}/lyrics/words`), requests[0]);
});

test('[if] the auto-cue cache is asked for a stick id [then] it settles the typed USB_NOT_SUPPORTED code, never FETCH_FAILED', async () => {
	const cache = await loadTypeScriptModule('src/lib/components/rb/deck/auto-cues-cache.svelte.ts', {
		viteApiBase: API_BASE
	});
	cache.ensureAutoCues(STICK_ID);
	await settle();
	assert.deepEqual(cache.getAutoCuesEntry(STICK_ID), { status: 'error', code: 'USB_NOT_SUPPORTED' });
	assert.deepEqual(requests, []);

	globalThis.fetch = unreachableDaemon();
	cache.ensureAutoCues(LIBRARY_ID);
	await settle();
	assert.deepEqual(cache.getAutoCuesEntry(LIBRARY_ID), { status: 'error', code: 'TEST_DAEMON_UNREACHABLE' });
	assert.deepEqual(requests, [`${API_BASE}/api/v1/tracks/${LIBRARY_ID}/auto-cues`]);
});

test('[if] a stem waveform is asked for a stick id [then] the cache settles USB_NOT_SUPPORTED with no request', async () => {
	const stems = await loadTypeScriptModule('src/lib/components/rb/wave/stem-waveform-cache.svelte.ts', {
		viteApiBase: API_BASE
	});
	stems.ensureStemWaveform(STICK_ID, 'vocals');
	await settle();
	assert.deepEqual(stems.getStemWaveformEntry(STICK_ID, 'vocals'), { status: 'error', code: 'USB_NOT_SUPPORTED' });
	assert.deepEqual(requests, []);

	globalThis.fetch = unreachableDaemon();
	stems.ensureStemWaveform(LIBRARY_ID, 'vocals');
	await settle();
	assert.deepEqual(stems.getStemWaveformEntry(LIBRARY_ID, 'vocals'), { status: 'error', code: 'TEST_DAEMON_UNREACHABLE' });
	assert.deepEqual(requests, [`${API_BASE}/api/v1/tracks/${LIBRARY_ID}/stems/vocals/waveform`]);
});

//-----------------------------------------------------------------------------
// analysis source: the confirmation loop and the switch refresh skip a stick
//-----------------------------------------------------------------------------

test('[if] the beatgrid lane is own and a stick /anlz answers rekordbox [then] it is confirmed on the first answer and cached, while a library rekordbox answer is not', async () => {
	const cache = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache.svelte.ts', {
		viteApiBase: API_BASE
	});
	cache.analysisSourceState.features.beatgrid = 'own';
	try {
		const stick = gridlessAnlz(STICK_ID, 'rekordbox');
		const library = gridlessAnlz(LIBRARY_ID, 'rekordbox');
		assert.equal(cache.anlzMatchesConfirmedSource(stick), true);
		assert.equal(cache.anlzMatchesConfirmedSource(library), false, 'control: a library rekordbox answer under own still disagrees');

		// Bounded: without the exemption this loop never returns, so the third
		// call rejects and the test fails instead of hanging.
		let calls = 0;
		const confirmed = await cache.fetchAnlzUntilSourceConfirmed(async () => {
			calls += 1;
			if (calls > 2) throw new Error('fetchAnlzUntilSourceConfirmed kept re-fetching a stick payload');
			return stick;
		});
		assert.equal(calls, 1, 'a stick payload is confirmed on its first answer');
		assert.equal(confirmed, stick);

		cache.refreshAnlzCacheEntry(STICK_ID, stick);
		cache.refreshAnlzCacheEntry(LIBRARY_ID, library);
		assert.equal(cache.isAnlzEntryUsable(cache.getAnlzEntry(STICK_ID)), true, 'the stick answer is cached');
		assert.equal(cache.getAnlzEntry(LIBRARY_ID), undefined, 'control: a disagreeing library answer is evicted');
		assert.deepEqual(requests, []);
	} finally {
		delete cache.analysisSourceState.features.beatgrid;
	}
});

test('[if] the analysis source switches with a stick deck and a library deck loaded [then] only the library track is re-fetched', async () => {
	const refresh = await loadTypeScriptModule('src/lib/components/rb/wave/analysis-source-refresh.ts', {
		viteApiBase: API_BASE
	});
	const fetchedAnlz = [];
	const ports = {
		invalidateAllAnlzCacheEntries: () => {},
		refreshAnlzCacheEntry: () => {},
		notifyGridlessSettlement: () => {},
		getReadyAnlz: () => null,
		fetchAnlzBypassingHttpCache: async (stable_id) => {
			fetchedAnlz.push(stable_id);
			throw new Error('this test stops at the staging fetch');
		}
	};
	const deck = (stable_id) => ({ stable_id, anlz: null, bpm: null, key: null });

	const stickOnly = await refresh.refreshAnalysisSourceDecks([1, 2], { 1: deck(STICK_ID), 2: deck(null) }, ports);
	assert.equal(stickOnly, null, 'nothing was fetched, so nothing was served');
	assert.deepEqual(fetchedAnlz, []);
	assert.deepEqual(requests, []);

	globalThis.fetch = unreachableDaemon();
	await assert.rejects(
		refresh.refreshAnalysisSourceDecks([1, 2], { 1: deck(STICK_ID), 2: deck(LIBRARY_ID) }, ports)
	);
	assert.deepEqual(fetchedAnlz, [LIBRARY_ID], 'control: the library deck is still re-fetched');
	assert.ok(requests.every((url) => !url.includes(STICK_ID)), `no request names the stick id: ${requests}`);
});

test('[if] a gridless stick deck settles its beatgrid [then] it settles gridless with no rb-meta or fallback request', async () => {
	const upgrade = await loadTypeScriptModule('src/lib/player/beatgrid-upgrade.ts', { viteApiBase: API_BASE });
	const settled = [];
	const st = { anlz: gridlessAnlz(STICK_ID, 'rekordbox'), anlz_error: null };
	await upgrade.upgradeDeckBeatgrid(1, STICK_ID, st, () => false, (deck, landed) => {
		settled.push({ deck, landed });
	});
	assert.deepEqual(settled, [{ deck: 1, landed: false }]);
	assert.deepEqual(requests, []);

	globalThis.fetch = unreachableDaemon();
	const libraryDeck = { anlz: gridlessAnlz(LIBRARY_ID, 'rekordbox'), anlz_error: null };
	await upgrade.upgradeDeckBeatgrid(1, LIBRARY_ID, libraryDeck, () => false, () => {});
	assert.ok(
		requests.some((url) => url.includes(`/api/v1/tracks/${LIBRARY_ID}/`)),
		`control: a gridless library deck still asks the library routes: ${requests}`
	);
});

//-----------------------------------------------------------------------------
// hot cue writes on a stick deck (spec decision 2)
//-----------------------------------------------------------------------------

// Driven through the performance IPC (the agent and MIDI entry point) end to
// end. The refusal itself lives at the one write boundary every caller shares,
// api-rb.ts saveHotCue/clearHotCue/restoreHotCue: a second copy inside
// performance-ipc was measured redundant (removing it left this test green),
// so it is not duplicated there.
test('[if] hot_cue_clear or hot_cue_restore is dispatched for a stick deck [then] it rejects USB_READ_ONLY with no request, and a library deck still writes', async () => {
	const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', { viteApiBase: API_BASE });
	// The IPC module reads /api/v1/settings on its own; only per-track
	// requests are what this test is about.
	globalThis.fetch = unreachableDaemon();
	const trackRequests = () => requests.filter((url) => url.includes('/tracks/'));
	let loaded = STICK_ID;
	globalThis.window = {};
	const resetDriver = ipc.installPerformanceHotCueDriverForTest({
		stableId: () => loaded,
		refresh: async () => {},
		hasRbMapping: () => false
	});
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		for (const command of [
			{ type: 'hot_cue_clear', deck: 1, slot: 'A', revision: 'etag' },
			{ type: 'hot_cue_restore', deck: 1, slot: 'A', revision: 'etag', reversal_id: 'token' }
		]) {
			await assert.rejects(window.musicDjToolsPerformance.dispatch(command), (error) => {
				assert.equal(error.name, 'UsbTrackRefusal', `${command.type}: ${error.message}`);
				assert.equal(error.code, 'USB_READ_ONLY');
				return true;
			});
		}
		assert.deepEqual(trackRequests(), []);

		loaded = LIBRARY_ID;
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'hot_cue_clear', deck: 1, slot: 'A', revision: 'etag' })
		);
		assert.deepEqual(trackRequests(), [`${API_BASE}/api/v1/tracks/${LIBRARY_ID}/hot-cues/A`], 'control: a library clear still writes');
	} finally {
		uninstall();
		resetDriver();
		delete globalThis.window;
	}
});

//-----------------------------------------------------------------------------
// load failure wording
//-----------------------------------------------------------------------------

test('[if] a stick load fails with a USB code [then] the toast headline is plain words, and any other code keeps the generic headline', async () => {
	const harness = await loadTypeScriptModule('tests/unit/fixtures/stick-load-failure-toast-entry.ts', {
		viteApiBase: API_BASE
	});
	globalThis.fetch = async () => new Response(null, { status: 204 });
	const response = new Response(null, { status: 404 });
	const cases = [
		[new harness.ApiError(404, 'USB_STICK_NOT_MOUNTED', 'stick not mounted', response), 'Stick removed - plug it back in to load this track'],
		[new harness.RbApiError(404, 'USB_FILE_MISSING', 'file missing'), "This track's audio file is missing from the stick"],
		[new harness.RbApiError(404, 'USB_TRACK_NOT_FOUND', 'no such track'), "This track is no longer in the stick's rekordbox export"]
	];
	for (const [cause, headline] of cases) {
		harness.reportDeckLoadFailure(2, `${STICK_ID}: ${cause.message}`, cause, {});
		const toast = harness.toasts.at(-1);
		assert.equal(toast.headline, headline);
		assert.equal(toast.message, `Deck 2 load failed - ${STICK_ID}: ${cause.message}`, 'the technical text is kept for detail');
	}

	for (const cause of [
		new harness.RbApiError(404, 'AUDIO_FILE_MISSING', 'gone'),
		new Error('USB_STICK_NOT_MOUNTED'),
		{ code: 'toString' }
	]) {
		harness.reportDeckLoadFailure(2, 'Night Ride: gone', cause, {});
		const headline = harness.toasts.at(-1).headline;
		assert.ok(
			!cases.some(([, words]) => words === headline),
			`control: ${JSON.stringify(cause.code ?? cause.message)} must not borrow a stick headline, got ${headline}`
		);
	}
});

//-----------------------------------------------------------------------------
// suggest-next (source-level: the repo has no component mount harness)
//-----------------------------------------------------------------------------

test('[if] SuggestNextStrip builds its request [then] stick ids are filtered from the session and a stick deck 1 renders inert with a tooltip', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/SuggestNextStrip.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(source, /const session = withoutUsbTrackIds\(sessionIds\.length > 0 \? sessionIds : localSessionIds\);/);
	assert.match(source, /if \(sid === null \|\| isUsbTrackId\(sid\)\) \{\s*stripState = \{ kind: sid === null \? 'idle' : 'stick' \};\s*oncandidates\?\.\(\[\]\);\s*return;/);
	assert.match(source, /body: \{ stable_id: sid, session_ids: session,/, 'the filtered list is the one sent');
	assert.match(source, /\{:else if stripState\.kind === 'stick'\}\s*<span class="dim" title="[^"]+">/);
});
