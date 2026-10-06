// LIBM-171: the library is held once in the browser and every list resolves
// from what is held, so switching views is instant instead of a re-walk.
//
// Regression one-liners:
//   - if two concurrent index loads make two requests then broken
//   - if a held, current index is fetched again on an All Tracks switch then broken
//   - if switching back to a held playlist refetches more than one revalidation page then broken
//   - if a moved membership ETag does not refill the playlist then broken (mutation control)
//   - if a library change does not drop held playlists then broken
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let contract;
let index;
let allTracks;
let playlists;

before(async () => {
	contract = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	index = await loadTypeScriptModule('src/lib/rb/library-index.ts');
	allTracks = await loadTypeScriptModule('src/lib/components/rb/browser/fill-all-tracks.ts');
	playlists = await loadTypeScriptModule('src/lib/components/rb/browser/fill-playlist-pane.ts');
});

beforeEach(() => {
	index.resetLibraryIndexForTests();
	playlists.clearPlaylistRowCache();
});

function mapRow(item, order) {
	return { stable_id: String(item), order };
}

function indexServer(items) {
	const calls = { count: 0 };
	index.setFetchTrackIndexForTests(async () => {
		calls.count += 1;
		return { revision: `r${calls.count}`, items: [...items] };
	});
	return calls;
}

test('concurrent loads share one request and a held index makes none', async () => {
	const calls = indexServer(['a', 'b']);
	const [one, two] = await Promise.all([index.loadLibraryIndex(), index.loadLibraryIndex()]);
	assert.equal(calls.count, 1);
	assert.equal(one, two);
	await index.loadLibraryIndex();
	assert.equal(calls.count, 1);
	assert.equal(index.libraryIndexIsCurrent(), true);
	index.invalidateLibraryIndex();
	assert.equal(index.libraryIndexIsCurrent(), false);
	assert.deepEqual(index.peekLibraryIndex().items, ['a', 'b'], 'the stale copy is still there to paint');
	await index.loadLibraryIndex();
	assert.equal(calls.count, 2);
});

test('All Tracks paints a held current index with no request at all', async () => {
	const pane = contract.createPaneStore();
	const seq = pane.beginLoad('all', 'All Tracks');
	let loads = 0;
	let firstPages = 0;
	await allTracks.fillAllTracksFromIndex({
		pane,
		seq,
		held: { items: ['a', 'b', 'c'], current: true },
		loadIndex: async () => {
			loads += 1;
			return [];
		},
		firstPage: async () => {
			firstPages += 1;
			return { items: [], next_cursor: null };
		},
		mapRow,
		progressTotal: 3
	});
	assert.equal(loads + firstPages, 0);
	assert.deepEqual(pane.rows.map((r) => r.stable_id), ['a', 'b', 'c']);
	assert.equal(pane.loading, false);
	assert.equal(pane.load_progress, null);
});

test('a stale held index paints at once, then the current index replaces it', async () => {
	const pane = contract.createPaneStore();
	const seq = pane.beginLoad('all', 'All Tracks');
	let paintedRows = null;
	await allTracks.fillAllTracksFromIndex({
		pane,
		seq,
		held: { items: ['old'], current: false },
		loadIndex: async () => ['a', 'b'],
		firstPage: async () => assert.fail('a held index never asks for a first page'),
		mapRow,
		progressTotal: null,
		onFirstPaint: () => {
			paintedRows = pane.rows.map((r) => r.stable_id);
		}
	});
	assert.deepEqual(paintedRows, ['old']);
	assert.deepEqual(pane.rows.map((r) => r.stable_id), ['a', 'b']);
});

test('with nothing held, the first page paints and ONE index request completes the list', async () => {
	const pane = contract.createPaneStore();
	const seq = pane.beginLoad('all', 'All Tracks');
	let releaseIndex;
	const gate = new Promise((resolve) => {
		releaseIndex = resolve;
	});
	let loads = 0;
	const fill = allTracks.fillAllTracksFromIndex({
		pane,
		seq,
		held: null,
		loadIndex: async () => {
			loads += 1;
			await gate;
			return ['a', 'b', 'c', 'd'];
		},
		firstPage: async () => ({ items: ['a', 'b'], next_cursor: 'b' }),
		mapRow,
		progressTotal: 4,
		onFirstPaint: () => releaseIndex()
	});
	await fill;
	assert.equal(loads, 1);
	assert.deepEqual(pane.rows.map((r) => r.stable_id), ['a', 'b', 'c', 'd']);
	assert.equal(pane.load_progress, null);
});

function playlistServer(n, etagRef) {
	const members = Array.from({ length: n }, (_, i) => `m${i}`);
	const calls = [];
	const fetchPage = async (offset, limit) => {
		calls.push({ offset, limit });
		const tracks = members.slice(offset, offset + limit);
		const next = offset + tracks.length;
		return {
			page: { tracks, total: n, next_offset: next >= n ? null : next },
			etag: etagRef.value
		};
	};
	return { calls, fetchPage };
}

async function openPlaylist(fetchPage) {
	const pane = contract.createPaneStore();
	const seq = pane.beginLoad('pl-1', 'Warmup');
	await playlists.fillPlaylistPane({
		pane,
		seq,
		fetchPage,
		mapRow,
		progressTotal: null,
		cacheKey: 'pl-1'
	});
	return pane;
}

test('switching back to a held playlist is one revalidation page, never a full refetch', async () => {
	const etag = { value: '"e1"' };
	const server = playlistServer(1200, etag);
	await openPlaylist(server.fetchPage);
	const fullFill = server.calls.length;
	assert.ok(fullFill >= 3, 'the first visit fills the whole playlist');
	server.calls.length = 0;
	const pane = await openPlaylist(server.fetchPage);
	assert.deepEqual(server.calls, [{ offset: 0, limit: playlists.PLAYLIST_FIRST_PAGE }]);
	assert.equal(pane.rows.length, 1200);
	assert.equal(pane.etag, '"e1"');
	assert.equal(pane.load_progress, null);
});

test('mutation control: a moved membership ETag refills the playlist', async () => {
	const etag = { value: '"e1"' };
	const server = playlistServer(1200, etag);
	await openPlaylist(server.fetchPage);
	server.calls.length = 0;
	etag.value = '"e2"';
	const pane = await openPlaylist(server.fetchPage);
	assert.ok(server.calls.length >= 3, `expected a refill, saw ${JSON.stringify(server.calls)}`);
	assert.equal(pane.rows.length, 1200);
	assert.equal(pane.etag, '"e2"');
});

test('a library change drops every held playlist', async () => {
	const etag = { value: '"e1"' };
	const server = playlistServer(600, etag);
	await openPlaylist(server.fetchPage);
	playlists.clearPlaylistRowCache();
	server.calls.length = 0;
	await openPlaylist(server.fetchPage);
	assert.equal(server.calls[0].limit, playlists.PLAYLIST_FIRST_PAGE);
	assert.ok(server.calls.length >= 2, 'nothing held, so the playlist fills from the server');
});
