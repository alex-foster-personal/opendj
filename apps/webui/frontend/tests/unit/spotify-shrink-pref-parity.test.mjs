/**
 * SPOT-6555b: the side player's "shrink Open DJ" choice is not UI-only. The
 * checkbox writes it to ui-prefs as well as localStorage, so
 * PUT /api/v1/ui-prefs reads and changes the same setting, and a value set
 * that way reaches the panel on the next hydrate.
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const API_BASE = 'https://spotify-shrink-pref.example.test';

function _fakeWindow() {
	const store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, v)
		}
	};
	return store;
}

/** A GET /api/v1/ui-prefs body always carries these keys. */
const REQUIRED_PREFS = { key_notation: 'camelot', library_hidden_columns: [], library_key_filter: [] };

function _respondWith(body, seen) {
	globalThis.fetch = async (request) => {
		if (request.method === 'PUT') seen.push(await request.clone().json());
		return new Response(JSON.stringify({ ...REQUIRED_PREFS, ...body }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
}

const originalFetch = globalThis.fetch;
afterEach(() => {
	globalThis.fetch = originalFetch;
	delete globalThis.window;
});

test('turning the shrink off persists locally and PUTs it to ui-prefs', async () => {
	const store = _fakeWindow();
	const puts = [];
	_respondWith({}, puts);
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	prefs.setSpotifyShrinkMain(false);
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(JSON.parse(store.get(STORAGE_KEY)).spotify_library.shrink_main_for_player, false);
	assert.deepEqual(puts, [{ spotify_shrink_main_for_player: false }]);
	// Setting the same value again writes nothing.
	prefs.setSpotifyShrinkMain(false);
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(puts.length, 1);
});

test('a value changed over HTTP hydrates into the panel pref', async () => {
	_fakeWindow();
	_respondWith({ spotify_shrink_main_for_player: false }, []);
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	prefs.uiPrefs.spotify_library = { pinned_ids: ['p'], recent_ids: [], shrink_main_for_player: true };
	await prefs.hydrateConfirmPrefsFromDisk();
	assert.deepEqual(prefs.uiPrefs.spotify_library, {
		pinned_ids: ['p'],
		recent_ids: [],
		shrink_main_for_player: false
	});
});

test('a disk body without the key leaves the local choice alone', async () => {
	_fakeWindow();
	_respondWith({ theme: 'dark' }, []);
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	prefs.uiPrefs.spotify_library = { pinned_ids: [], recent_ids: [], shrink_main_for_player: false };
	await prefs.hydrateConfirmPrefsFromDisk();
	assert.equal(prefs.uiPrefs.spotify_library.shrink_main_for_player, false);
});
