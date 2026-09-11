import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://lyrics-words.example.test';

// lyrics-cache maps getTrackLyricsWords 404 -> state 'none' (honest no-data).

let cache;
let originalFetch;

before(async () => {
	cache = await loadTypeScriptModule('src/lib/lyrics/lyrics-cache.svelte.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
	globalThis.window = {
		localStorage: {
			getItem: () => null,
			setItem: () => {}
		}
	};
});

after(() => {
	globalThis.fetch = originalFetch;
	delete globalThis.window;
});

test('loadLyrics caches 404 from getTrackLyricsWords as none', async () => {
	globalThis.fetch = async (request) => {
		assert.match(String(request.url), /\/api\/v1\/tracks\/missing-track-id\/lyrics\/words(\?include=lines)?$/);
		return new Response(JSON.stringify({ detail: 'no live lyric_verdict row for missing-track-id' }), {
			status: 404,
			statusText: 'Not Found',
			headers: { 'content-type': 'application/json' }
		});
	};

	await cache.loadLyrics('missing-track-id');
	const entry = cache.lyricEntry('missing-track-id');
	assert.ok(entry !== null, `expected lyric entry, got ${JSON.stringify(entry)}`);
	assert.equal(entry.state, 'none', entry.error ?? 'no error message');
	assert.equal(entry.track, null);
	assert.equal(entry.error, null);
});
