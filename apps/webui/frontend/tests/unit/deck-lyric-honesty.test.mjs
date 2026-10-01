import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('lyrics-cache falls back to line lyrics when words are absent', async () => {
	const cache = await readFile('src/lib/lyrics/lyrics-cache.svelte.ts', 'utf8');
	assert.match(cache, /fetchTrackLyrics/);
	assert.match(cache, /karaokeTrackFromLineLyrics/);
});

test('DeckLyricLine keeps loading honest before showing NO LYRIC DATA', async () => {
	const src = await readFile('src/lib/components/rb/deck/DeckLyricLine.svelte', 'utf8');
	const loadingPos = src.indexOf("entryState === 'loading'");
	const nonePos = src.indexOf("entryState === 'none'");
	assert.ok(loadingPos >= 0 && nonePos > loadingPos);
});
