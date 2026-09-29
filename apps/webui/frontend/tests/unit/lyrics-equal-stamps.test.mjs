// requirement: two lyric lines sung at the same moment are valid lyrics.
//
// The server's lyrics cache accepts equal start_ms (apps/lyrics/cache.py:
// "Equal stamps are valid LRC"), and the LRCLIB parser keeps doubled lines.
// The deck's parser used to reject them, so a cached track with a duet line
// rendered as a lyrics load error instead of its lyrics.
//
// [if] two consecutive lines share start_ms [then] fetchTrackLyrics returns
//   both lines
// [if] a line starts BEFORE the previous one [then] it is still rejected
//   (control: the fix must not accept out-of-order lyrics)
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://rekordbox-api.example.test';
let api;
let originalFetch;

function serveLines(lines) {
	globalThis.fetch = async () =>
		Response.json({ stable_id: 'sid-l', source: 'lrclib', lines });
}

before(async () => {
	api = await loadTypeScriptModule('src/lib/rb/api-rb.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('equal start_ms lines load as lyrics, not as a load error', async () => {
	serveLines([
		{ start_ms: 1000, text: 'Lead' },
		{ start_ms: 1000, text: 'Backing' },
		{ start_ms: 2500, text: 'Next' }
	]);
	const lyrics = await api.fetchTrackLyrics('sid-l');
	assert.deepEqual(
		lyrics.lines.map((line) => line.text),
		['Lead', 'Backing', 'Next']
	);
});

test('control: a line that goes backwards in time is still rejected', async () => {
	serveLines([
		{ start_ms: 2000, text: 'Later' },
		{ start_ms: 1000, text: 'Earlier' }
	]);
	await assert.rejects(api.fetchTrackLyrics('sid-l'), /lyrics line 1 is invalid/);
});
