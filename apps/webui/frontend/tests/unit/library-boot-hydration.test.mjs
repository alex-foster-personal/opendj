// requirement: PERF-UI-03
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let hydration;
let prefsCalls = 0;
let tracksCalls = 0;
let playlistsCalls = 0;

before(async () => {
	hydration = await loadTypeScriptModule('src/lib/rb/library-boot-hydration.ts');
});

afterEach(() => {
	prefsCalls = 0;
	tracksCalls = 0;
	playlistsCalls = 0;
	hydration.resetLibraryBootHydrationForTests();
});

test('startLibraryBootHydration is idempotent and shares tracks, prefs, and playlists promises', async () => {
	hydration.setPrefsHydratorForTests(async () => {
		prefsCalls += 1;
	});
	hydration.setFetchBootTracksPageForTests(() => {
		tracksCalls += 1;
		return Promise.resolve({ items: [], next_cursor: null });
	});
	hydration.setFetchBootPlaylistsForTests(() => {
		playlistsCalls += 1;
		return Promise.resolve([]);
	});
	hydration.startLibraryBootHydration();
	hydration.startLibraryBootHydration();
	const first = hydration.bootTracksPrefetch();
	const second = hydration.bootTracksPrefetch();
	assert.equal(first.tracksPromise, second.tracksPromise);
	assert.equal(first.prefsPromise, second.prefsPromise);
	assert.equal(first.playlistsPromise, second.playlistsPromise);
	assert.equal(tracksCalls, 1);
	assert.equal(prefsCalls, 1);
	assert.equal(playlistsCalls, 1);
	assert.ok(first.startedAt <= Date.now());
});

test('fetchBootTracksFirstPage falls back when prefetch rejects', async () => {
	hydration.setPrefsHydratorForTests(async () => {});
	hydration.setFetchBootTracksPageForTests(() => Promise.reject(new Error('boot fail')));
	hydration.setFetchBootPlaylistsForTests(() => Promise.resolve([]));
	let fallbackCalls = 0;
	hydration.setFallbackTracksFetchForTests(() => {
		fallbackCalls += 1;
		return Promise.resolve({ items: [{ stable_id: 't1' }], next_cursor: null });
	});
	hydration.setFallbackPlaylistsFetchForTests(() => Promise.resolve([]));
	hydration.startLibraryBootHydration();
	const page = await hydration.fetchBootTracksFirstPage(undefined);
	assert.equal(fallbackCalls, 1);
	assert.equal(page.items[0].stable_id, 't1');
});

test('bootPlaylistsPrefetch falls back to full list when fast prefetch rejects', async () => {
	hydration.setPrefsHydratorForTests(async () => {});
	hydration.setFetchBootTracksPageForTests(() => Promise.resolve({ items: [], next_cursor: null }));
	hydration.setFetchBootPlaylistsForTests(() => Promise.reject(new Error('fast validation fail')));
	let fallbackCalls = 0;
	hydration.setFallbackPlaylistsFetchForTests(() => {
		fallbackCalls += 1;
		return Promise.resolve([{ playlist_id: 'pl-1', name: 'Strict', track_count: 1, available_count: 1 }]);
	});
	hydration.startLibraryBootHydration();
	const lists = await hydration.bootPlaylistsPrefetch();
	assert.equal(fallbackCalls, 1);
	assert.equal(lists[0].playlist_id, 'pl-1');
});

test('canBootAllTracksEarly refuses playlist and spotify deep-link boots', () => {
	assert.equal(
		hydration.canBootAllTracksEarly({
			remembered: null,
			source: 'collection',
			spotify_selected_id: null
		}),
		true
	);
	assert.equal(
		hydration.canBootAllTracksEarly({
			remembered: { kind: 'playlist' },
			source: 'collection',
			spotify_selected_id: null
		}),
		false
	);
	assert.equal(
		hydration.canBootAllTracksEarly({
			remembered: null,
			source: 'spotify',
			spotify_selected_id: 'pl-1'
		}),
		false
	);
});
