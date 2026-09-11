import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { shouldFetchTrackLyrics } = await loadTypeScriptModule(
	'src/lib/components/rb/wave/lyrics-cached-ids.ts'
);

test('shouldFetchTrackLyrics waits for the cached-id index', () => {
	assert.equal(shouldFetchTrackLyrics('track-a', null), false);
});

test('shouldFetchTrackLyrics skips tracks absent from the cached-id index', () => {
	assert.equal(shouldFetchTrackLyrics('track-a', new Set(['track-b'])), false);
});

test('shouldFetchTrackLyrics allows tracks present in the cached-id index', () => {
	assert.equal(shouldFetchTrackLyrics('track-a', new Set(['track-a'])), true);
});
