/**
 * Contract tests for the shared lyric cache (src/lib/lyrics/lyrics-cache.svelte.ts).
 *
 * The cache is the ONE place word payloads live in RAM, and every lyric
 * surface (panel, deck line, waveform lanes, library tip) reads through it.
 * Its whole value is that a track is fetched once and that a known-empty
 * track is never re-requested, so those are what is pinned here.
 *
 * No module is stubbed: the real `$lib/api` and the real generated OpenAPI
 * client are bundled, and only `globalThis.fetch` - the actual network
 * boundary - is replaced. The client forwards fetch through a closure for
 * exactly this reason, so a wrong URL or a mis-decoded status fails here
 * rather than passing against a hand-written fake.
 *
 * Regression lines:
 * - if a 404 stops mapping to state 'none' then every surface re-fans-out
 *   requests for tracks the pipeline has nothing for
 * - if a 500 maps to 'none' instead of 'error' then a broken daemon reads as
 *   an empty library
 * - if two concurrent loads stop sharing one promise then a playlist reveal
 *   fires N duplicate requests per track
 * - if loadLyrics stops sending include=lines then the cache holds
 *   half-filled variants and the deck line renders nothing
 * - if applyVerdict rewrites more than the verdict then an override silently
 *   drops the words it was judging
 * - if evictAllExcept drops a kept id then a loaded deck loses its lyrics
 *   mid-set
 * - if cacheStats stops counting words then the RAM readout cannot answer
 *   "how much is this holding"
 */
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://lyrics-cache.example.test';
const MODULE = 'src/lib/lyrics/lyrics-cache.svelte.ts';

const originalFetch = globalThis.fetch;

after(() => {
	globalThis.fetch = originalFetch;
});

/** A fresh module instance per test: the cache is module-level state by
 * design, so sharing one instance would make the tests order-dependent.
 *
 * The module's operations are one exported facade (`lyricsCache`), so the
 * handle these tests drive is that object widened with the module's own
 * constants - every call below still lands on the real production function. */
async function freshCache() {
	const mod = await loadTypeScriptModule(MODULE, { viteApiBase: API_BASE });
	return { ...mod.lyricsCache, HOVER_DEBOUNCE_MS: mod.HOVER_DEBOUNCE_MS };
}

function verdict(stableId, overrides = {}) {
	return {
		stable_id: stableId,
		verdict: 'vocal',
		effective_verdict: 'vocal',
		coverage_pct: 42.5,
		source: 'lrclib:exact',
		language_iso3: 'eng',
		n_words: 3,
		n_lines: 1,
		pct_witness_red: 0.1,
		has_words: true,
		override: null,
		override_note: null,
		pipeline_version: 'r6',
		computed_at: '2026-09-01T00:00:00Z',
		updated_at: '2026-09-01T00:00:01Z',
		...overrides
	};
}

function karaokeTrack(stableId, wordCount) {
	const words = Array.from({ length: wordCount }, (_, idx) => ({
		idx,
		word: `w${idx}`,
		start_s: idx,
		end_s: idx + 0.5,
		score: 0.9,
		witness: 'agree',
		line_final: idx === wordCount - 1
	}));
	return {
		verdict: verdict(stableId, { n_words: wordCount }),
		words,
		lines: [
			{
				first_idx: 0,
				last_idx: wordCount - 1,
				text: words.map((w) => w.word).join(' '),
				start_s: 0,
				end_s: wordCount,
				n_words: wordCount,
				n_red: 0,
				n_judged: wordCount,
				quality: 1,
				band: 'good',
				para_final: true
			}
		]
	};
}

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

test('a payload lands as loaded, with lines requested', async () => {
	const cache = await freshCache();
	const seen = [];
	globalThis.fetch = async (request) => {
		seen.push(request.url);
		return jsonResponse(karaokeTrack('t-loaded', 3));
	};

	await cache.load('t-loaded');

	const entry = cache.entry('t-loaded');
	assert.equal(entry.state, 'loaded');
	assert.equal(entry.error, null);
	assert.equal(entry.track.words.length, 3);
	assert.equal(entry.track.lines.length, 1);
	assert.equal(seen.length, 1);
	assert.equal(
		seen[0],
		`${API_BASE}/api/v1/tracks/t-loaded/lyrics/words?include=lines`,
		'loadLyrics must always ask for lines, on the words route'
	);
});

test('lines-only words payload lands as loaded without line-cache fetch', async () => {
	const cache = await freshCache();
	let calls = 0;
	globalThis.fetch = async (request) => {
		calls += 1;
		const url = request.url;
		if (url.includes('/lyrics/words')) {
			return jsonResponse({
				verdict: verdict('t-lines-only', { n_words: 0, has_words: false, n_lines: 2 }),
				words: [],
				lines: [
					{
						first_idx: 0,
						last_idx: 0,
						text: 'first line',
						start_s: 0,
						end_s: 2,
						n_words: 0,
						n_red: 0,
						n_judged: 0,
						quality: null,
						band: 'unjudged',
						para_final: false
					},
					{
						first_idx: 1,
						last_idx: 1,
						text: 'second line',
						start_s: 2,
						end_s: 4,
						n_words: 0,
						n_red: 0,
						n_judged: 0,
						quality: null,
						band: 'unjudged',
						para_final: true
					}
				]
			});
		}
		return jsonResponse({ detail: 'unexpected' }, 404);
	};

	await cache.load('t-lines-only');

	const entry = cache.entry('t-lines-only');
	assert.equal(entry.state, 'loaded');
	assert.equal(entry.track.lines.length, 2);
	assert.equal(calls, 1, 'line-cache must not run when words route already returned lines');
});

test('a 404 lands as none, not as an error', async () => {
	const cache = await freshCache();
	globalThis.fetch = async () =>
		jsonResponse({ detail: 'no karaoke lyric data for track' }, 404);

	await cache.load('t-missing');

	const entry = cache.entry('t-missing');
	assert.equal(entry.state, 'none');
	assert.equal(entry.track, null);
	assert.equal(entry.error, null);
});

test('a non-404 failure lands as error and keeps the message', async () => {
	const cache = await freshCache();
	globalThis.fetch = async () =>
		jsonResponse({ detail: { code: 'LYRICS_READ_FAILED', message: 'state.db is locked' } }, 500);

	await cache.load('t-broken');

	const entry = cache.entry('t-broken');
	assert.equal(entry.state, 'error');
	assert.equal(entry.track, null);
	assert.equal(entry.error, 'state.db is locked');
});

test('an unreachable daemon lands as error, not as none', async () => {
	const cache = await freshCache();
	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};

	await cache.load('t-offline');

	const entry = cache.entry('t-offline');
	assert.equal(entry.state, 'error');
	assert.equal(entry.error, 'fetch failed');
});

test('concurrent loads of one id share a single request', async () => {
	const cache = await freshCache();
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(karaokeTrack('t-once', 2));
	};

	await Promise.all([
		cache.load('t-once'),
		cache.load('t-once'),
		cache.load('t-once')
	]);
	await cache.load('t-once');

	assert.equal(calls, 1, 'a loaded or in-flight track must never be refetched');
	assert.equal(cache.entry('t-once').state, 'loaded');
});

test('a known-empty track is not refetched', async () => {
	const cache = await freshCache();
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse({ detail: 'nothing here' }, 404);
	};

	await cache.load('t-empty');
	assert.equal(calls, 2, 'words 404 then line-cache 404 before settling on none');
	await cache.load('t-empty');
	assert.equal(calls, 2, "state 'none' must stop the re-request fan-out");
});

test('applyVerdict replaces the verdict and nothing else', async () => {
	const cache = await freshCache();
	globalThis.fetch = async () => jsonResponse(karaokeTrack('t-override', 3));
	await cache.load('t-override');
	const wordsBefore = cache.entry('t-override').track.words;

	cache.applyVerdict(
		't-override',
		verdict('t-override', { override: 'no-lyrics', effective_verdict: 'no-lyrics' })
	);

	const entry = cache.entry('t-override');
	assert.equal(entry.state, 'loaded');
	assert.equal(entry.track.verdict.effective_verdict, 'no-lyrics');
	assert.equal(entry.track.verdict.override, 'no-lyrics');
	assert.deepEqual(entry.track.words, wordsBefore, 'the words must survive an override');
	assert.equal(entry.track.lines.length, 1, 'the lines must survive an override');
});

test('applyVerdict is a no-op for a track that was never loaded', async () => {
	const cache = await freshCache();

	cache.applyVerdict('t-absent', verdict('t-absent'));

	assert.equal(cache.entry('t-absent'), null);
});

test('evictAllExcept keeps exactly the listed ids', async () => {
	const cache = await freshCache();
	globalThis.fetch = async (request) => {
		const id = new URL(request.url).pathname.split('/')[4];
		return jsonResponse(karaokeTrack(id, 2));
	};
	await cache.load('keep-a');
	await cache.load('keep-b');
	await cache.load('drop-c');

	cache.evictAllExcept(['keep-a', 'keep-b']);

	assert.notEqual(cache.entry('keep-a'), null);
	assert.notEqual(cache.entry('keep-b'), null);
	assert.equal(cache.entry('drop-c'), null);
	assert.equal(cache.stats().entries, 2);
});

test('cacheStats counts entries, loaded tracks and words', async () => {
	const cache = await freshCache();
	globalThis.fetch = async (request) => {
		const id = new URL(request.url).pathname.split('/')[4];
		if (id === 'stat-empty') return jsonResponse({ detail: 'nothing here' }, 404);
		return jsonResponse(karaokeTrack(id, id === 'stat-a' ? 3 : 5));
	};
	await cache.load('stat-a');
	await cache.load('stat-b');
	await cache.load('stat-empty');

	assert.deepEqual(cache.stats(), { entries: 3, loaded: 2, words: 8 });
});

test('the hover path is debounced and cancellable', async () => {
	const cache = await freshCache();
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse(karaokeTrack('t-hover', 1));
	};

	cache.hoverLoad('t-hover');
	cache.cancelHover('t-hover');
	await new Promise((resolve) => setTimeout(resolve, cache.HOVER_DEBOUNCE_MS + 20));

	assert.equal(calls, 0, 'a cancelled hover must never reach the network');
	assert.equal(cache.entry('t-hover'), null);
});
