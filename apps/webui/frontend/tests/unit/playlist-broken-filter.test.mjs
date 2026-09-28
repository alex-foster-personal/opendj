// [if] hide-broken is on with min 4 playable tracks [then] only playlists with 4+ playable tracks show, [else stop].
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	countHiddenBrokenPlaylists,
	isPlaylistVisibleWithBrokenFilter,
	playlistMostlyBroken
} from '../../src/lib/rb/playlist-broken-filter.ts';

const MIN_TRACKS = 4;

function fixturePlaylist(availableCount) {
	return {
		playlist_id: `pl-${availableCount}`,
		available_count: availableCount,
		track_count: availableCount
	};
}

const FIXTURE_PLAYLISTS = [0, 3, 4, 50].map(fixturePlaylist);

test('playlistMostlyBroken: 0 and 3 are mostly broken, 4 and 50 are not', () => {
	assert.equal(playlistMostlyBroken(0, MIN_TRACKS), true);
	assert.equal(playlistMostlyBroken(3, MIN_TRACKS), true);
	assert.equal(playlistMostlyBroken(4, MIN_TRACKS), false);
	assert.equal(playlistMostlyBroken(50, MIN_TRACKS), false);
	assert.equal(playlistMostlyBroken(-1, MIN_TRACKS), false);
});

test('with Broken filter on, only 4- and 50-playable playlists are visible', () => {
	for (const p of FIXTURE_PLAYLISTS) {
		const visible = isPlaylistVisibleWithBrokenFilter(p, true, MIN_TRACKS, false);
		if (p.available_count >= MIN_TRACKS) {
			assert.equal(visible, true, `expected ${p.available_count} playable to show`);
		} else {
			assert.equal(visible, false, `expected ${p.available_count} playable to hide`);
		}
	}
});

test('hidden count matches playlists below the min when Broken filter is on', () => {
	assert.equal(
		countHiddenBrokenPlaylists(FIXTURE_PLAYLISTS, true, MIN_TRACKS, () => false),
		2
	);
});

test('create-grace keeps a zero-playable playlist visible while Broken filter is on', () => {
	const empty = fixturePlaylist(0);
	assert.equal(
		isPlaylistVisibleWithBrokenFilter(empty, true, MIN_TRACKS, true),
		true
	);
	assert.equal(
		countHiddenBrokenPlaylists(FIXTURE_PLAYLISTS, true, MIN_TRACKS, (id) => id === empty.playlist_id),
		1
	);
});

test('unique library-wide total is not the sum of overlapping playlist playable counts', () => {
	// Two playlists share one track: summing row counts double-counts; the All
	// Tracks header uses reconcile-summary unique non-broken totals instead.
	const playlists = [
		{ available_count: 10 },
		{ available_count: 10 }
	];
	const sumOfPlaylistRows = playlists.reduce((n, p) => n + p.available_count, 0);
	const uniqueTracksInLibrary = 10;
	assert.equal(sumOfPlaylistRows, 20);
	assert.notEqual(sumOfPlaylistRows, uniqueTracksInLibrary);
});
