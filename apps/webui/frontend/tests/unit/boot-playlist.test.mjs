import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Boot pane selection: /performance must not open on a blank track table.
// Regression lines:
// - if resolveBootPlaylist returns null for a non-empty library then every
//   boot shows an empty pane again -- the original bug, broken
// - if it returns All Tracks for an EMPTY library then an empty table is
//   dressed up as a real selection and reads as a failed load -- broken
// - if a remembered playlist that still exists is not restored then the
//   persistence is decorative -- broken
// - if a remembered playlist that has since been deleted is restored anyway
//   then boot loads a dangling id straight into an error -- broken
// - if last_playlist is absent from a stored blob and that throws instead of
//   defaulting, every existing user's prefs break on upgrade -- broken
// - if a malformed last_playlist is silently reset instead of throwing then
//   the fail-fast prefs policy is gone -- broken
// - if setLastPlaylist persists on an unchanged identity then every pane
//   load writes localStorage for nothing -- broken
//
// Pin 2ac3a0 (playlist deck-membership tints + CURRENT fold):
// - if a non-selected playlist holding a deck-loaded track isn't tinted then
//   the pin's "which playlists are already open" cue is missing -- broken
// - if a playlist open in 2+ pane tabs doesn't get the DARKER tint then the
//   two states are indistinguishable -- broken
// - if the selected row's own selection colour doesn't win over both tints
//   then the active playlist looks like just another open one -- broken
// - if computeTreeCurrentFold doesn't fire 'above'/'below' exactly when the
//   selected row scrolls outside the tree viewport then CURRENT never shows,
//   or shows on top of a row that is already visible -- broken

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

/** Install a localStorage-backed fake window so prefs._load reads `raw`. */
function _fakeWindow(raw) {
	const store = new Map();
	if (raw !== undefined) store.set(STORAGE_KEY, raw);
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, v)
		}
	};
	return store;
}

afterEach(() => {
	delete globalThis.window;
});

async function _loadContract() {
	return loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
}

// The boot decision lives in its own runeless module; pane-contract re-exports
// only the two functions BrowserPanel calls, so the All Tracks identity is read
// from the source module here.
async function _loadBootPane() {
	return loadTypeScriptModule('src/lib/components/rb/browser/boot-pane-selection.ts');
}

async function _loadPrefs() {
	return loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
}

// ------------------------------------------------------- resolveBootPlaylist

test('a non-empty library with nothing remembered boots on All Tracks', async () => {
	const contract = await _loadContract();

	const choice = contract.resolveBootPlaylist({
		remembered: null,
		known_playlist_ids: ['pl-1', 'pl-2'],
		all_tracks_count: 8355
	});

	assert.deepEqual(choice, { playlist_id: 'all', name: 'All Tracks', kind: 'all_tracks' });
});

test('an empty library boots on nothing rather than a fake All Tracks', async () => {
	const contract = await _loadContract();

	for (const count of [0, -1, Number.NaN]) {
		assert.equal(
			contract.resolveBootPlaylist({
				remembered: null,
				known_playlist_ids: [],
				all_tracks_count: count
			}),
			null,
			`all_tracks_count ${count} must not produce a selection`
		);
	}
});

test('an empty library ignores even a remembered playlist', async () => {
	const contract = await _loadContract();

	const choice = contract.resolveBootPlaylist({
		remembered: { playlist_id: 'pl-1', name: 'Warmup', kind: 'playlist' },
		known_playlist_ids: ['pl-1'],
		all_tracks_count: 0
	});

	assert.equal(choice, null);
});

test('a remembered playlist that still exists is restored by identity', async () => {
	const contract = await _loadContract();

	const choice = contract.resolveBootPlaylist({
		remembered: { playlist_id: 'pl-2', name: 'Peak', kind: 'playlist' },
		known_playlist_ids: ['pl-1', 'pl-2'],
		all_tracks_count: 120
	});

	assert.deepEqual(choice, { playlist_id: 'pl-2', name: 'Peak', kind: 'playlist' });
});

test('a remembered playlist that no longer exists falls back to All Tracks', async () => {
	const contract = await _loadContract();
	const bootPane = await _loadBootPane();

	const choice = contract.resolveBootPlaylist({
		remembered: { playlist_id: 'pl-deleted', name: 'Gone', kind: 'playlist' },
		known_playlist_ids: ['pl-1'],
		all_tracks_count: 120
	});

	assert.deepEqual(choice, bootPane.ALL_TRACKS_CHOICE);
});

test('a remembered All Tracks resolves to All Tracks without consulting the tree', async () => {
	const contract = await _loadContract();
	const bootPane = await _loadBootPane();

	const choice = contract.resolveBootPlaylist({
		remembered: { playlist_id: 'all', name: 'All Tracks', kind: 'all_tracks' },
		known_playlist_ids: [],
		all_tracks_count: 3
	});

	assert.deepEqual(choice, bootPane.ALL_TRACKS_CHOICE);
});

// -------------------------------------------------------- last_playlist pref

test('first run remembers no playlist', async () => {
	_fakeWindow(); // no stored blob at all = genuine first run
	const prefs = await _loadPrefs();

	assert.equal(prefs.uiPrefs.last_playlist, null);
});

test('a blob written before this field existed still loads', async () => {
	_fakeWindow(JSON.stringify({ hide_broken_links: true }));
	const prefs = await _loadPrefs();

	assert.equal(prefs.uiPrefs.last_playlist, null);
	assert.equal(prefs.uiPrefs.hide_broken_links, true);
});

test('a persisted playlist identity round-trips', async () => {
	_fakeWindow(
		JSON.stringify({
			hide_broken_links: false,
			last_playlist: { playlist_id: 'pl-7', name: 'Closers', kind: 'playlist' }
		})
	);
	const prefs = await _loadPrefs();

	assert.deepEqual(prefs.uiPrefs.last_playlist, {
		playlist_id: 'pl-7',
		name: 'Closers',
		kind: 'playlist'
	});
});

test('a malformed last_playlist throws rather than silently resetting', async () => {
	const bad = [
		{ playlist_id: '', name: 'x', kind: 'playlist' },
		{ playlist_id: 'pl-1', name: 'x', kind: 'folder' },
		{ playlist_id: 'pl-1', name: 7, kind: 'playlist' },
		{ name: 'x', kind: 'playlist' },
		'all'
	];
	for (const value of bad) {
		_fakeWindow(JSON.stringify({ hide_broken_links: false, last_playlist: value }));
		await assert.rejects(
			async () => _loadPrefs(),
			/malformed prefs blob \(last_playlist/,
			`${JSON.stringify(value)} must be rejected loudly`
		);
		delete globalThis.window;
	}
});

test('setLastPlaylist persists a change and skips an unchanged identity', async () => {
	const store = _fakeWindow(JSON.stringify({ hide_broken_links: false }));
	const prefs = await _loadPrefs();

	prefs.setLastPlaylist({ playlist_id: 'all', name: 'All Tracks', kind: 'all_tracks' });
	assert.deepEqual(JSON.parse(store.get(STORAGE_KEY)).last_playlist, {
		playlist_id: 'all',
		name: 'All Tracks',
		kind: 'all_tracks'
	});

	// An unchanged identity must not rewrite the key: _loadPane calls this on
	// every load, including the boot restore of what was just read.
	store.set(STORAGE_KEY, 'SENTINEL-NOT-REWRITTEN');
	prefs.setLastPlaylist({ playlist_id: 'all', name: 'All Tracks', kind: 'all_tracks' });
	assert.equal(store.get(STORAGE_KEY), 'SENTINEL-NOT-REWRITTEN');

	// A real change does write again.
	prefs.setLastPlaylist({ playlist_id: 'pl-3', name: 'Openers', kind: 'playlist' });
	assert.deepEqual(JSON.parse(store.get(STORAGE_KEY)).last_playlist, {
		playlist_id: 'pl-3',
		name: 'Openers',
		kind: 'playlist'
	});
});

// -------------------------------------------------------- spotify_library pref

test('a blob without spotify_library loads empty pin/recent lists', async () => {
	_fakeWindow(JSON.stringify({ hide_broken_links: true }));
	const prefs = await _loadPrefs();

	assert.deepEqual(prefs.uiPrefs.spotify_library, { pinned_ids: [], recent_ids: [] });
});

test('a persisted spotify_library identity round-trips', async () => {
	_fakeWindow(
		JSON.stringify({
			hide_broken_links: false,
			spotify_library: { pinned_ids: ['pl-pin'], recent_ids: ['pl-recent', 'pl-older'] }
		})
	);
	const prefs = await _loadPrefs();

	assert.deepEqual(prefs.uiPrefs.spotify_library, {
		pinned_ids: ['pl-pin'],
		recent_ids: ['pl-recent', 'pl-older']
	});
});

test('a malformed spotify_library throws rather than silently resetting', async () => {
	const bad = ['nope', { pinned_ids: [1] }, { recent_ids: '' }];
	for (const value of bad) {
		_fakeWindow(JSON.stringify({ hide_broken_links: false, spotify_library: value }));
		await assert.rejects(
			async () => _loadPrefs(),
			/spotify_library/,
			`${JSON.stringify(value)} must be rejected loudly`
		);
		delete globalThis.window;
	}
});

test('rememberSpotifyRecent persists prepend and skips rewrite when already first', async () => {
	const store = _fakeWindow(JSON.stringify({ hide_broken_links: false }));
	const prefs = await _loadPrefs();

	prefs.rememberSpotifyRecent('pl-1');
	assert.deepEqual(JSON.parse(store.get(STORAGE_KEY)).spotify_library.recent_ids, ['pl-1']);

	prefs.rememberSpotifyRecent('pl-2');
	assert.deepEqual(JSON.parse(store.get(STORAGE_KEY)).spotify_library.recent_ids, [
		'pl-2',
		'pl-1'
	]);

	store.set(STORAGE_KEY, 'SENTINEL-NOT-REWRITTEN');
	prefs.rememberSpotifyRecent('pl-2');
	assert.equal(store.get(STORAGE_KEY), 'SENTINEL-NOT-REWRITTEN');
});

test('toggleSpotifyPinned add then remove', async () => {
	const store = _fakeWindow(JSON.stringify({ hide_broken_links: false }));
	const prefs = await _loadPrefs();

	prefs.toggleSpotifyPinned('pl-pin');
	assert.deepEqual(JSON.parse(store.get(STORAGE_KEY)).spotify_library.pinned_ids, ['pl-pin']);

	prefs.toggleSpotifyPinned('pl-pin');
	assert.deepEqual(JSON.parse(store.get(STORAGE_KEY)).spotify_library.pinned_ids, []);
});

// ------------------------------------- playlist deck-membership tints (2ac3a0)

function _pane(overrides = {}) {
	return { playlist_id: null, rows: [], ...overrides };
}

test('a non-selected playlist holding a loaded-deck track gets the light deck tint', async () => {
	const contract = await _loadContract();
	const panes = [_pane({ playlist_id: 'pl-1', rows: [{ stable_id: 'a' }, { stable_id: 'b' }] })];
	const deckIds = new Set(['b']);
	const membership = contract.derivePlaylistDeckMembership(panes, deckIds);
	assert.deepEqual([...membership], ['pl-1']);

	const tint = contract.playlistTintOf({
		playlist_id: 'pl-1',
		selected: false,
		deckLoadedPlaylistIds: membership,
		multiPanePlaylistIds: new Set()
	});
	assert.equal(tint, 'deck');
});

test('a playlist open in 2+ pane tabs gets the darker multi tint', async () => {
	const contract = await _loadContract();
	const panes = [
		_pane({ playlist_id: 'pl-1' }),
		_pane({ playlist_id: 'pl-1' }),
		_pane({ playlist_id: 'pl-2' })
	];
	const counts = contract.derivePlaylistPaneOpenCounts(panes);
	assert.equal(counts.get('pl-1'), 2);
	assert.equal(counts.get('pl-2'), 1);
	const multi = contract.multiPanePlaylistIds(counts);
	assert.deepEqual([...multi], ['pl-1']);

	assert.equal(
		contract.playlistTintOf({
			playlist_id: 'pl-1',
			selected: false,
			deckLoadedPlaylistIds: new Set(),
			multiPanePlaylistIds: multi
		}),
		'multi'
	);
	assert.equal(
		contract.playlistTintOf({
			playlist_id: 'pl-2',
			selected: false,
			deckLoadedPlaylistIds: new Set(),
			multiPanePlaylistIds: multi
		}),
		'none'
	);
});

test('multi tint outranks deck tint when both apply, and the selected row takes neither', async () => {
	const contract = await _loadContract();
	const both = { deckLoadedPlaylistIds: new Set(['pl-1']), multiPanePlaylistIds: new Set(['pl-1']) };
	assert.equal(
		contract.playlistTintOf({ playlist_id: 'pl-1', selected: false, ...both }),
		'multi'
	);
	// the selected-colour precedence: selection always wins over both tints
	assert.equal(
		contract.playlistTintOf({ playlist_id: 'pl-1', selected: true, ...both }),
		'none'
	);
});

test('All Tracks and blank panes never count toward playlist deck membership or open counts', async () => {
	const contract = await _loadContract();
	const panes = [
		_pane({ playlist_id: 'all', rows: [{ stable_id: 'a' }] }),
		_pane({ playlist_id: null, rows: [{ stable_id: 'a' }] })
	];
	assert.equal(contract.derivePlaylistDeckMembership(panes, new Set(['a'])).size, 0);
	assert.equal(contract.derivePlaylistPaneOpenCounts(panes).size, 0);
});

test('Missing Tracks never counts toward playlist deck membership or open counts', async () => {
	const contract = await _loadContract();
	const panes = [_pane({ playlist_id: 'missing', rows: [{ stable_id: 'a' }] })];
	assert.equal(contract.derivePlaylistDeckMembership(panes, new Set(['a'])).size, 0);
	assert.equal(contract.derivePlaylistPaneOpenCounts(panes).size, 0);
});

// --------------------------------------------------- CURRENT fold (2ac3a0)

test('computeTreeCurrentFold: selected row above the viewport folds "above"', async () => {
	const contract = await _loadContract();
	const fold = contract.computeTreeCurrentFold({
		selectedTop: 0,
		selectedBottom: 20,
		scrollTop: 100,
		viewportHeight: 300
	});
	assert.equal(fold, 'above');
});

test('computeTreeCurrentFold: selected row below the viewport folds "below"', async () => {
	const contract = await _loadContract();
	const fold = contract.computeTreeCurrentFold({
		selectedTop: 500,
		selectedBottom: 520,
		scrollTop: 0,
		viewportHeight: 300
	});
	assert.equal(fold, 'below');
});

test('computeTreeCurrentFold: selected row on screen folds null (no CURRENT control)', async () => {
	const contract = await _loadContract();
	const fold = contract.computeTreeCurrentFold({
		selectedTop: 100,
		selectedBottom: 120,
		scrollTop: 0,
		viewportHeight: 300
	});
	assert.equal(fold, null);
});

test('computeTreeCurrentFold: no selected row (e.g. a smartlist, or nothing selected) folds null', async () => {
	const contract = await _loadContract();
	const fold = contract.computeTreeCurrentFold({
		selectedTop: null,
		selectedBottom: null,
		scrollTop: 0,
		viewportHeight: 300
	});
	assert.equal(fold, null);
});
