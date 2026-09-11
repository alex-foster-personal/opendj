import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let rank;

before(async () => {
	rank = await loadTypeScriptModule('src/lib/rb/spotify-playlist-rank.ts');
});

function row(overrides = {}) {
	return {
		playlist_id: 'pl-1',
		name: 'Pleasure',
		...overrides
	};
}

function ranked(playlists, args = {}) {
	return rank.rankSpotifyPlaylists(playlists, {
		query: '',
		pinnedIds: [],
		recentIds: [],
		...args
	});
}

test("'pleasure' matches 'Pleasure' (case-insensitive name match)", () => {
	assert.equal(rank.playlistNameMatches('Pleasure', 'pleasure'), true);
});

test("'ure' matches 'Pleasure' (substring, not token)", () => {
	assert.equal(rank.playlistNameMatches('Pleasure', 'ure'), true);
});

test("'xyz' against a 3-row list returns []", () => {
	const playlists = [
		row({ playlist_id: 'a', name: 'Pleasure' }),
		row({ playlist_id: 'b', name: 'Warmup' }),
		row({ playlist_id: 'c', name: 'Closers' })
	];
	assert.deepEqual(ranked(playlists, { query: 'xyz' }), []);
});

test('whitespace-only query returns every row', () => {
	const playlists = [
		row({ playlist_id: 'a', name: 'Pleasure' }),
		row({ playlist_id: 'b', name: 'Warmup' }),
		row({ playlist_id: 'c', name: 'Closers' })
	];
	assert.equal(ranked(playlists, { query: '   ' }).length, 3);
	assert.equal(rank.normalizePlaylistQuery('   '), '');
});

test('name filter ignores playlist_id', () => {
	const playlists = [row({ playlist_id: 'xyz-secret', name: 'Pleasure' })];
	assert.deepEqual(ranked(playlists, { query: 'xyz' }), []);
	assert.equal(ranked(playlists, { query: 'Pleas' }).length, 1);
});

test('pinned ids come first in pin-list order; unpinned follow', () => {
	const playlists = [
		row({ playlist_id: 'a', name: 'Alpha' }),
		row({ playlist_id: 'b', name: 'Bravo' }),
		row({ playlist_id: 'c', name: 'Charlie' })
	];
	const ids = ranked(playlists, { pinnedIds: ['c', 'a'] }).map((p) => p.playlist_id);
	assert.deepEqual(ids, ['c', 'a', 'b']);
});

test('a pinned row whose name misses the query is absent', () => {
	const playlists = [
		row({ playlist_id: 'pin', name: 'Pinned leftover' }),
		row({ playlist_id: 'hit', name: 'Pleasure' })
	];
	const ids = ranked(playlists, { query: 'pleasure', pinnedIds: ['pin'] }).map(
		(p) => p.playlist_id
	);
	assert.deepEqual(ids, ['hit']);
});

test('recents sit after pins, most-recent first, skipping ids not in the input', () => {
	const playlists = [
		row({ playlist_id: 'a', name: 'Alpha' }),
		row({ playlist_id: 'b', name: 'Bravo' }),
		row({ playlist_id: 'c', name: 'Charlie' }),
		row({ playlist_id: 'd', name: 'Delta' })
	];
	const ids = ranked(playlists, {
		pinnedIds: ['d'],
		recentIds: ['gone', 'b', 'c']
	}).map((p) => p.playlist_id);
	assert.deepEqual(ids, ['d', 'b', 'c', 'a']);
});

test('owned: true outranks unowned when both unpinned/unrecent', () => {
	const playlists = [
		row({ playlist_id: 'a', name: 'Alpha', owned: false }),
		row({ playlist_id: 'b', name: 'Bravo', owned: true }),
		row({ playlist_id: 'c', name: 'Charlie' })
	];
	const ids = ranked(playlists).map((p) => p.playlist_id);
	assert.equal(ids[0], 'b');
});

test('rows with updated_at sort newer-first among otherwise equal leftovers; missing does not crash', () => {
	const playlists = [
		row({ playlist_id: 'old', name: 'Same', updated_at: '2026-01-01T00:00:00Z' }),
		row({ playlist_id: 'new', name: 'Same', updated_at: '2026-07-24T00:00:00Z' }),
		row({ playlist_id: 'none', name: 'Same', updated_at: null })
	];
	const ids = ranked(playlists).map((p) => p.playlist_id);
	assert.deepEqual(ids, ['new', 'old', 'none']);
});

test('prependRecentId caps at 12, dedupes, no-ops when already first', () => {
	assert.equal(rank.SPOTIFY_RECENT_CAP, 12);
	const first = rank.prependRecentId(['a'], 'b');
	assert.deepEqual(first, ['b', 'a']);
	const same = rank.prependRecentId(first, 'b');
	assert.equal(same, first);
	const moved = rank.prependRecentId(['a', 'b', 'c'], 'c');
	assert.deepEqual(moved, ['c', 'a', 'b']);
	const overflow = Array.from({ length: 12 }, (_, i) => `id-${i}`);
	const capped = rank.prependRecentId(overflow, 'fresh');
	assert.equal(capped.length, 12);
	assert.equal(capped[0], 'fresh');
	assert.equal(capped.at(-1), 'id-10');
});

test('togglePinnedId add/remove/cap', () => {
	assert.equal(rank.SPOTIFY_PINNED_CAP, 50);
	const added = rank.togglePinnedId(['a'], 'b');
	assert.deepEqual(added, ['a', 'b']);
	assert.deepEqual(rank.togglePinnedId(added, 'a'), ['b']);
	const full = Array.from({ length: 50 }, (_, i) => `pin-${i}`);
	const noop = rank.togglePinnedId(full, 'extra');
	assert.equal(noop, full);
	assert.equal(rank.togglePinnedId(full, 'pin-0').length, 49);
});

test('empty id throws on prepend/toggle', () => {
	assert.throws(() => rank.prependRecentId([], ''), /non-empty string/);
	assert.throws(() => rank.togglePinnedId([], ''), /non-empty string/);
});

test('508-row dogfood size: filter pl-500 returns one row; ranking 508 does not throw', () => {
	const playlists = Array.from({ length: 508 }, (_, i) =>
		row({ playlist_id: `id-${i}`, name: `pl-${i}` })
	);
	assert.doesNotThrow(() => ranked(playlists));
	assert.equal(ranked(playlists).length, 508);
	const hits = ranked(playlists, { query: 'pl-500' });
	assert.equal(hits.length, 1);
	assert.equal(hits[0].name, 'pl-500');
});
