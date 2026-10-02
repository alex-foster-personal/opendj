// requirement: PERF-UI-05
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let prefetch;
let fill;

before(async () => {
	prefetch = await loadTypeScriptModule('src/lib/rb/library-playlist-page-prefetch.ts');
	fill = await loadTypeScriptModule('src/lib/components/rb/browser/fill-playlist-pane.ts');
});

const emptyPage = (etag) =>
	Promise.resolve({ page: { tracks: [], total: 0, next_offset: null }, etag });

test('the prefetch page size is the fill first-page size, so a switch can join it', () => {
	assert.equal(prefetch.PLAYLIST_PREFETCH_PAGE_SIZE, fill.PLAYLIST_FIRST_PAGE);
});

test('prefetchFirstPage is idempotent and a switch joins its in-flight promise', async () => {
	let calls = 0;
	const p = prefetch.createPlaylistPagePrefetch(() => {
		calls += 1;
		return emptyPage('e1');
	});
	p.prefetchFirstPage('pl-a');
	p.prefetchFirstPage('pl-a');
	const first = await p.fetchFirstPage('pl-a', 0);
	assert.equal(calls, 1);
	assert.equal(first.etag, 'e1');
});

test('a prefetched page is joined once: switching back reads the route again', async () => {
	let calls = 0;
	const p = prefetch.createPlaylistPagePrefetch(() => {
		calls += 1;
		return emptyPage(`e${calls}`);
	});
	p.prefetchFirstPage('pl-a');
	const first = await p.fetchFirstPage('pl-a', 0);
	const second = await p.fetchFirstPage('pl-a', 0);
	assert.equal(calls, 2);
	assert.equal(first.etag, 'e1');
	assert.equal(second.etag, 'e2', "a revisit must not be served the first visit's rows");
});

test('a stale prefetch is not joined and a later hover refreshes it', async () => {
	let clock = 0;
	let calls = 0;
	const p = prefetch.createPlaylistPagePrefetch(
		() => {
			calls += 1;
			return emptyPage(`e${calls}`);
		},
		() => clock
	);
	p.prefetchFirstPage('pl-s');
	clock = prefetch.PLAYLIST_PREFETCH_MAX_AGE_MS + 1;
	p.prefetchFirstPage('pl-s');
	assert.equal(calls, 2, 'a hover after the max age starts a fresh prefetch');
	clock += prefetch.PLAYLIST_PREFETCH_MAX_AGE_MS + 1;
	const page = await p.fetchFirstPage('pl-s', 0);
	assert.equal(calls, 3);
	assert.equal(page.etag, 'e3');
});

test('control: a fresh prefetch is still joined just inside the max age', async () => {
	let clock = 0;
	let calls = 0;
	const p = prefetch.createPlaylistPagePrefetch(
		() => {
			calls += 1;
			return emptyPage('e');
		},
		() => clock
	);
	p.prefetchFirstPage('pl-f');
	clock = prefetch.PLAYLIST_PREFETCH_MAX_AGE_MS;
	await p.fetchFirstPage('pl-f', 0);
	assert.equal(calls, 1);
});

test('a prefetch of a different page size is not joined', async () => {
	const limits = [];
	const p = prefetch.createPlaylistPagePrefetch((_id, limit) => {
		limits.push(limit);
		return emptyPage('e');
	});
	p.prefetchFirstPage('pl-z', 30);
	await p.fetchFirstPage('pl-z', 0, 50);
	assert.deepEqual(limits, [30, 50]);
});

test('a cold switch whose live GET fails does not retry it', async () => {
	let calls = 0;
	const p = prefetch.createPlaylistPagePrefetch(() => {
		calls += 1;
		return Promise.reject(new Error('network exploded'));
	});
	await assert.rejects(p.fetchFirstPage('pl-x', 0), /network exploded/);
	assert.equal(calls, 1);
});

test('a switch joined to a pending prefetch reports its rejection without a second GET', async () => {
	let calls = 0;
	let rejectPrefetch;
	const p = prefetch.createPlaylistPagePrefetch(() => {
		calls += 1;
		return new Promise((_resolve, reject) => {
			rejectPrefetch = reject;
		});
	});
	p.prefetchFirstPage('pl-b');
	const switching = p.fetchFirstPage('pl-b', 0);
	rejectPrefetch(new Error('prefetch fail'));
	await assert.rejects(switching, /prefetch fail/);
	assert.equal(calls, 1);
});

test('a prefetch that rejected before the switch still reaches the switch', async () => {
	let calls = 0;
	const p = prefetch.createPlaylistPagePrefetch(() => {
		calls += 1;
		return Promise.reject(new Error('prefetch fail early'));
	});
	p.prefetchFirstPage('pl-b');
	await new Promise((resolve) => setTimeout(resolve, 0));
	await assert.rejects(p.fetchFirstPage('pl-b', 0), /prefetch fail early/);
	assert.equal(calls, 1);
});

test('control: the failed prefetch is reported once, then the next switch reads live', async () => {
	let calls = 0;
	const p = prefetch.createPlaylistPagePrefetch(() => {
		calls += 1;
		if (calls === 1) return Promise.reject(new Error('prefetch fail'));
		return Promise.resolve({
			page: { tracks: [{ stable_id: 't1' }], total: 1, next_offset: null },
			etag: 'e2'
		});
	});
	p.prefetchFirstPage('pl-b');
	await assert.rejects(p.fetchFirstPage('pl-b', 0), /prefetch fail/);
	const page = await p.fetchFirstPage('pl-b', 0);
	assert.equal(calls, 2);
	assert.equal(page.page.tracks[0].stable_id, 't1');
});

test('control: a stale failed prefetch is not reported; the switch reads live', async () => {
	let calls = 0;
	let clock = 0;
	const p = prefetch.createPlaylistPagePrefetch(
		() => {
			calls += 1;
			if (calls === 1) return Promise.reject(new Error('old failure'));
			return emptyPage('pl-b');
		},
		() => clock
	);
	p.prefetchFirstPage('pl-b');
	clock = prefetch.PLAYLIST_PREFETCH_MAX_AGE_MS + 1;
	await p.fetchFirstPage('pl-b', 0);
	assert.equal(calls, 2);
});

test('fetchFirstPage uses a live GET for a non-zero offset', async () => {
	let calls = 0;
	const p = prefetch.createPlaylistPagePrefetch((_id, _limit, offset) => {
		calls += 1;
		return Promise.resolve({
			page: { tracks: [], total: 30, next_offset: offset === 0 ? 30 : null },
			etag: `e-${offset}`
		});
	});
	const page = await p.fetchFirstPage('pl-c', 30);
	assert.equal(calls, 1);
	assert.equal(page.etag, 'e-30');
});

test('prefetchTreeIntent caps eager prefetches', () => {
	const seen = [];
	const p = prefetch.createPlaylistPagePrefetch((playlistId) => {
		seen.push(playlistId);
		return emptyPage(playlistId);
	});
	p.prefetchTreeIntent(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j'], 3);
	assert.deepEqual(seen, ['a', 'b', 'c']);
});
