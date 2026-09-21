// requirement: PERF-UI-05
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let prefetch;

before(async () => {
	prefetch = await loadTypeScriptModule('src/lib/rb/library-playlist-page-prefetch.ts');
});

afterEach(() => {
	prefetch.resetPlaylistPagePrefetchForTests();
});

test('prefetchPlaylistFirstPage is idempotent and shares one in-flight promise', async () => {
	let calls = 0;
	prefetch.setFetchPlaylistPageForTests(() => {
		calls += 1;
		return Promise.resolve({
			page: { tracks: [], total: 0, next_offset: null },
			etag: 'e1'
		});
	});
	prefetch.prefetchPlaylistFirstPage('pl-a');
	prefetch.prefetchPlaylistFirstPage('pl-a');
	const first = await prefetch.fetchPlaylistFirstPage('pl-a', 0);
	const second = await prefetch.fetchPlaylistFirstPage('pl-a', 0);
	assert.equal(calls, 1);
	assert.equal(first.etag, 'e1');
	assert.equal(second.etag, 'e1');
});

test('fetchPlaylistFirstPage falls back when prefetch rejects', async () => {
	let calls = 0;
	prefetch.setFetchPlaylistPageForTests(() => {
		calls += 1;
		if (calls === 1) return Promise.reject(new Error('prefetch fail'));
		return Promise.resolve({
			page: { tracks: [{ stable_id: 't1' }], total: 1, next_offset: null },
			etag: 'e2'
		});
	});
	prefetch.prefetchPlaylistFirstPage('pl-b');
	const page = await prefetch.fetchPlaylistFirstPage('pl-b', 0);
	assert.equal(calls, 2);
	assert.equal(page.page.tracks[0].stable_id, 't1');
});

test('fetchPlaylistFirstPage uses live GET for non-zero offset', async () => {
	let calls = 0;
	prefetch.setFetchPlaylistPageForTests((_id, _limit, offset) => {
		calls += 1;
		return Promise.resolve({
			page: { tracks: [], total: 30, next_offset: offset === 0 ? 30 : null },
			etag: `e-${offset}`
		});
	});
	const page = await prefetch.fetchPlaylistFirstPage('pl-c', 30);
	assert.equal(calls, 1);
	assert.equal(page.etag, 'e-30');
});

test('prefetchPlaylistTreeIntent caps eager prefetches', async () => {
	const seen = [];
	prefetch.setFetchPlaylistPageForTests((playlistId) => {
		seen.push(playlistId);
		return Promise.resolve({
			page: { tracks: [], total: 0, next_offset: null },
			etag: playlistId
		});
	});
	prefetch.prefetchPlaylistTreeIntent(
		['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j'],
		3
	);
	assert.deepEqual(seen, ['a', 'b', 'c']);
});
