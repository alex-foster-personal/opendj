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

	const choice = contract.resolveBootPlaylist({
		remembered: { playlist_id: 'pl-deleted', name: 'Gone', kind: 'playlist' },
		known_playlist_ids: ['pl-1'],
		all_tracks_count: 120
	});

	assert.deepEqual(choice, contract.ALL_TRACKS_CHOICE);
});

test('a remembered All Tracks resolves to All Tracks without consulting the tree', async () => {
	const contract = await _loadContract();

	const choice = contract.resolveBootPlaylist({
		remembered: { playlist_id: 'all', name: 'All Tracks', kind: 'all_tracks' },
		known_playlist_ids: [],
		all_tracks_count: 3
	});

	assert.deepEqual(choice, contract.ALL_TRACKS_CHOICE);
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
