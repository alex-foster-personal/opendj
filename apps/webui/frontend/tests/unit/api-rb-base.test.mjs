import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://rekordbox-api.example.test';
let api;
let originalFetch;
let requestedUrls;

before(async () => {
	api = await loadTypeScriptModule('src/lib/rb/api-rb.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
	requestedUrls = [];
	// api-rb.ts re-exports the track/playlist/health helpers from $lib/api,
	// which now call `fetch(request)` through the generated client, while
	// api-rb's own helpers still call `fetch(url, init)`. Record the URL from
	// whichever idiom the call arrived in.
	globalThis.fetch = async (input) => {
		requestedUrls.push(input instanceof Request ? input.url : String(input));
		return new Response('{}', {
			status: 200,
			headers: { 'content-type': 'application/json', etag: 'test-etag' }
		});
	};
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('performance metadata helpers share the configured Rekordbox API base', async () => {
	assert.equal(api.RB_API_BASE, API_BASE);

	await api.getHealth();
	await api.listPlaylists();
	await api.getTrack('track / one');

	assert.deepEqual(requestedUrls, [
		`${API_BASE}/api/v1/health`,
		`${API_BASE}/api/v1/playlists`,
		`${API_BASE}/api/v1/tracks/track%20%2F%20one`
	]);
});

test('reconcile summary uses the generated endpoint and rejects impossible counts', async () => {
	globalThis.fetch = async (input) => {
		requestedUrls.push(input instanceof Request ? input.url : String(input));
		return Response.json({ total_tracks: 12, total_broken: 3, orphan_broken: 0, playlists: [] });
	};
	assert.deepEqual(await api.getReconcileSummary(), {
		total_tracks: 12,
		total_broken: 3,
		orphan_broken: 0,
		playlists: []
	});
	assert.equal(requestedUrls.at(-1), `${API_BASE}/api/v1/reconcile/summary?cached=true`);

	globalThis.fetch = async () =>
		Response.json({ total_tracks: 2, total_broken: 3, orphan_broken: 0, playlists: [] });
	await assert.rejects(api.getReconcileSummary(), /invalid total_tracks or total_broken counts/);
});

test('HEALTH-15: a summary with no scan yet is warming, never zero counts', async () => {
	// [if] the engine answers computed_at null [then] ReconcileSummaryWarming, [else stop].
	const warming = {
		total_tracks: null,
		total_broken: null,
		orphan_broken: null,
		playlists: [],
		availability: null,
		computed_at: null,
		age_s: null,
		refreshing: true,
		refresh_error: null
	};
	globalThis.fetch = async () => Response.json(warming);
	await assert.rejects(api.getReconcileSummary(), (error) => error instanceof api.ReconcileSummaryWarming);
	// Mutation control: a finished scan with counts is a summary, not "still warming".
	globalThis.fetch = async () =>
		Response.json({ ...warming, total_tracks: 4, total_broken: 1, orphan_broken: 0, computed_at: 1.5, age_s: 2 });
	assert.equal((await api.getReconcileSummary()).total_broken, 1);
	// A server error is a failure, not warming.
	globalThis.fetch = async () => Response.json({ detail: { code: 'BOOM', message: 'x' } }, { status: 500 });
	await assert.rejects(api.getReconcileSummary(), (error) => !(error instanceof api.ReconcileSummaryWarming));
});

test('stem artifact probe validates the exact real Demucs contract', async () => {
	globalThis.fetch = async (input) => {
		requestedUrls.push(String(input));
		return Response.json({
			schema: 1,
			stable_id: 'track-1',
			source: 'demucs',
			model: 'htdemucs',
			layout: 'demucs4',
			sample_rate_hz: 44_100,
			frame_count: 441_000,
			channel_count: 2,
			parts: {
				vocals: { media_type: 'audio/wav' },
				drums: { media_type: 'audio/wav' },
				bass: { media_type: 'audio/wav' },
				other: { media_type: 'audio/wav' }
			}
		});
	};

	const probe = await api.probeStemArtifact('track-1');
	assert.equal(probe.status, 'ready');
	assert.equal(probe.manifest.model, 'htdemucs');
	assert.equal(
		requestedUrls.at(-1),
		`${API_BASE}/api/v1/tracks/track-1/stems`
	);
});

test('missing stem artifacts stay explicit while malformed successful payloads fail', async () => {
	globalThis.fetch = async () =>
		Response.json(
			{ detail: { code: 'STEM_BUNDLE_NOT_FOUND', message: 'no real artifacts' } },
			{ status: 404 }
		);
	assert.deepEqual(await api.probeStemArtifact('track-2'), {
		status: 'unavailable',
		error: 'STEM_BUNDLE_NOT_FOUND: no real artifacts'
	});

	globalThis.fetch = async () => Response.json({ schema: 1, stable_id: 'track-2' });
	await assert.rejects(api.probeStemArtifact('track-2'), /source must be demucs/i);
});

test('HTTP 200 unavailable stem envelope is explicit unavailable, not a ready manifest', async () => {
	globalThis.fetch = async () =>
		Response.json({
			status: 'unavailable',
			code: 'STEM_BUNDLE_NOT_FOUND',
			stable_id: 'track-2',
			message: 'no stored bundle'
		});
	assert.deepEqual(await api.probeStemArtifact('track-2'), {
		status: 'unavailable',
		error: 'STEM_BUNDLE_NOT_FOUND: no stored bundle'
	});
});

const ROFORMER_MANIFEST = {
	schema: 1,
	stable_id: 'track-3',
	source: 'roformer',
	model: 'mel-band-roformer',
	layout: 'roformer2',
	sample_rate_hz: 44_100,
	frame_count: 441_000,
	channel_count: 2,
	parts: {
		vocals: { media_type: 'audio/mpeg' },
		instrumental: { media_type: 'audio/mpeg' }
	}
};

test('a two-part RoFormer manifest is a first-class contract, not a broken Demucs one', async () => {
	globalThis.fetch = async () => Response.json(ROFORMER_MANIFEST);
	const probe = await api.probeStemArtifact('track-3');
	assert.equal(probe.status, 'ready');
	assert.equal(probe.manifest.layout, 'roformer2');
	assert.deepEqual(Object.keys(probe.manifest.parts), ['vocals', 'instrumental']);
});

test('a manifest that does not declare its layout is rejected, never assumed', async () => {
	// Guessing demucs4 here would make the deck fetch drums/bass/other from a
	// bundle that has neither, and fail deep in decode instead of at the edge.
	const { layout: _omitted, ...undeclared } = ROFORMER_MANIFEST;
	globalThis.fetch = async () => Response.json(undeclared);
	await assert.rejects(api.probeStemArtifact('track-3'), /layout must be demucs4 or roformer2/i);

	globalThis.fetch = async () => Response.json({ ...ROFORMER_MANIFEST, layout: 'roformer3' });
	await assert.rejects(api.probeStemArtifact('track-3'), /layout must be demucs4 or roformer2/i);
});

test('a manifest whose parts contradict its declared layout is rejected', async () => {
	globalThis.fetch = async () =>
		Response.json({ ...ROFORMER_MANIFEST, layout: 'demucs4' });
	await assert.rejects(api.probeStemArtifact('track-3'), /parts/i);
});

test('hot-cue client reads slot revisions and sends them on every mutation', async () => {
	const requests = [];
	globalThis.fetch = async (input, init = {}) => {
		requests.push({ url: String(input), init });
		return Response.json({
			cue: { slot: 'A', revision: 'next-revision' },
			revision: 'next-revision',
			reversal: { reversal_id: 'server-token' }
		});
	};

	await api.fetchHotCueSlots('track / one');
	await api.saveHotCue('track / one', 'A', 1000, 'revision-a');
	await api.clearHotCue('track / one', 'A', 'revision-b');
	await api.restoreHotCue('track / one', 'A', 'revision-c', 'server-token');

	assert.equal(requests[0].url, `${API_BASE}/api/v1/tracks/track%20%2F%20one/hot-cues`);
	assert.equal(requests[1].init.headers['If-Match'], 'revision-a');
	assert.equal(requests[2].init.headers['If-Match'], 'revision-b');
	assert.equal(requests[3].init.headers['If-Match'], 'revision-c');
	assert.deepEqual(JSON.parse(requests[3].init.body), { reversal_id: 'server-token' });
});

test('fetchAnlz never reuses a browser HTTP cache entry, while refresh still reloads', async () => {
	// The selected analysis source belongs to the daemon, not this browser
	// document. A prior tab can have cached RBX at gen=0 while a fresh tab
	// sees OWN at its own gen=0, so normal /anlz reads must not reuse HTTP
	// cache entries (discussion_r3923593660).
	const requests = [];
	globalThis.fetch = async (input, init = {}) => {
		requests.push({ url: String(input), init });
		return Response.json({
			stable_id: 'track-1',
			points: 38400,
			waveform: {
				kind: 'mono',
				preview: { length: 0, low: [], mid: [], high: [] },
				detail: { length: 0, low: [], mid: [], high: [] }
			},
			beatgrid: { beat_count: 0, beats: [] },
			cues: [],
			phrases: [],
			local_waveform: { status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 },
			vocals: { status: 'not_analyzed' }
		});
	};

	await api.fetchAnlz('track-1');
	assert.equal(
		requests[0].init.cache,
		'no-store',
		'the ordinary path must never adopt an /anlz payload cached by another document'
	);

	await api.fetchAnlzBypassingHttpCache('track-1');
	assert.equal(
		requests[1].init.cache,
		'reload',
		'the mutation-refresh path must force a real round trip past the HTTP cache'
	);
});

test('_throwRbApiError turns a text/plain 500 into an informative RbApiError', async () => {
	// A regression pin on `_throwRbApiError` itself, not a substitute for
	// exercising the real backend/#767 fault-injection scenario (rejected as
	// such on review, PR #860 -- see deckload-smoke.spec.ts's header for the
	// full UNAVAILABLE reasoning). starlette's ServerErrorMiddleware default
	// body for any truly unhandled exception is text/plain, not the
	// {"detail": {code, message}} contract every other error path here
	// assumes; that shape crashed `_throwRbApiError` into a bare, cryptic
	// SyntaxError on WebKit (#767).
	// Real production function (`fetchAudioArrayBuffer`), real `Response`
	// object, no application state fabricated -- the same fetch-stubbing
	// idiom every other test in this file already uses -- but a stubbed
	// `fetch` cannot detect a regression in the real server or middleware,
	// only in this function's own parsing. That narrower claim is what this
	// test makes.
	const serverErrorBody = `Internal Server Error${'!'.repeat(300)}`;
	globalThis.fetch = async () =>
		new Response(serverErrorBody, {
			status: 500,
			headers: { 'content-type': 'text/plain' }
		});

	await assert.rejects(api.fetchAudioArrayBuffer('track-1'), (error) => {
		assert.equal(error.name, 'RbApiError');
		assert.equal(error.status, 500);
		assert.equal(error.code, 'HTTP_500');
		assert.equal(error.message, `HTTP_500: ${serverErrorBody.slice(0, 200)}`);
		return true;
	});
});

test('_throwRbApiError preserves the backend detail code and message contract', async () => {
	globalThis.fetch = async () =>
		Response.json(
			{ detail: { code: 'AUDIO_FILE_MISSING', message: 'the file is gone' } },
			{ status: 404 }
		);

	await assert.rejects(api.fetchAudioArrayBuffer('track-1'), (error) => {
		assert.equal(error.name, 'RbApiError');
		assert.equal(error.status, 404);
		assert.equal(error.code, 'AUDIO_FILE_MISSING');
		assert.equal(error.message, 'AUDIO_FILE_MISSING: the file is gone');
		return true;
	});
});
