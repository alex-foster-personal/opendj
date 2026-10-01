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

test('bootPlaylistsPrefetch falls back to fast list when fast prefetch rejects', async () => {
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

// requirement: LIBM-138
function seedBootWalk(pages) {
	const asked = [];
	hydration.setPrefsHydratorForTests(async () => {});
	hydration.setFetchBootPlaylistsForTests(() => Promise.resolve([]));
	hydration.setFetchBootTracksPageForTests((limit, cursor) => {
		asked.push({ limit, cursor });
		const page = pages[cursor ?? 'first'];
		return page instanceof Error ? Promise.reject(page) : Promise.resolve(page);
	});
	return asked;
}

test('the boot walk asks for a small first page and full pages after it', async () => {
	const asked = seedBootWalk({
		first: { items: [{ stable_id: 't1' }], next_cursor: 'c1' },
		c1: { items: [{ stable_id: 't2' }], next_cursor: null }
	});
	hydration.startLibraryBootHydration();
	const first = await hydration.fetchBootTracksFirstPage(undefined);
	const second = await hydration.fetchBootTracksFirstPage(first.next_cursor);
	assert.deepEqual(asked, [
		{ limit: hydration.LIBRARY_BOOT_FIRST_PAGE_SIZE, cursor: undefined },
		{ limit: hydration.LIBRARY_BOOT_PAGE_SIZE, cursor: 'c1' }
	]);
	assert.equal(second.next_cursor, null);
	assert.ok(
		hydration.LIBRARY_BOOT_FIRST_PAGE_SIZE < hydration.LIBRARY_BOOT_PAGE_SIZE,
		'the first page is the one a person is waiting on'
	);
});

test('the boot walk is in flight until its last page arrives', async () => {
	seedBootWalk({
		first: { items: [], next_cursor: 'c1' },
		c1: { items: [], next_cursor: null }
	});
	assert.equal(hydration.bootListingWalkInFlight(), false, 'nothing started yet');
	hydration.startLibraryBootHydration();
	assert.equal(hydration.bootListingWalkInFlight(), true);
	const first = await hydration.fetchBootTracksFirstPage(undefined);
	assert.equal(hydration.bootListingWalkInFlight(), true, 'more pages are coming');
	await hydration.fetchBootTracksFirstPage(first.next_cursor);
	assert.equal(hydration.bootListingWalkInFlight(), false, 'the last page ends the walk');
});

test('a one-page library ends the walk on its first page', async () => {
	seedBootWalk({ first: { items: [], next_cursor: null } });
	hydration.startLibraryBootHydration();
	await hydration.fetchBootTracksFirstPage(undefined);
	assert.equal(hydration.bootListingWalkInFlight(), false);
});

test('a page that fails ends the walk and still rejects', async () => {
	seedBootWalk({
		first: { items: [], next_cursor: 'c1' },
		c1: new Error('engine went away')
	});
	hydration.startLibraryBootHydration();
	const first = await hydration.fetchBootTracksFirstPage(undefined);
	await assert.rejects(hydration.fetchBootTracksFirstPage(first.next_cursor), /engine went away/);
	assert.equal(hydration.bootListingWalkInFlight(), false, 'a dead walk must not hold boot work');
});

test('a boot that opens another pane can end the walk itself', () => {
	seedBootWalk({ first: { items: [], next_cursor: 'c1' } });
	hydration.startLibraryBootHydration();
	hydration.bootListingWalkSettled();
	hydration.bootListingWalkSettled();
	assert.equal(hydration.bootListingWalkInFlight(), false);
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
