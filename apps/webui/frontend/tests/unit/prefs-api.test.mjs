import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://prefs-api.example.test';

let prefs;
let originalFetch;

function installPrefsStorage(raw = null) {
	const values = new Map(raw === null ? [] : [['mdt.rb.ui-prefs.v1', raw]]);
	const originalWindow = globalThis.window;
	globalThis.window = {
		localStorage: {
			getItem: (key) => values.get(key) ?? null,
			setItem: (key, value) => values.set(key, value)
		}
	};
	return {
		values,
		restore: () => {
			if (originalWindow === undefined) delete globalThis.window;
			else globalThis.window = originalWindow;
		}
	};
}

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('setTheme fires PUT /api/v1/ui-prefs with the theme body', async () => {
	let seen;
	let body;
	let release;
	const gate = new Promise((resolve) => {
		release = resolve;
	});
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		release();
		return jsonResponse({ theme: 'dark', hide_todo_settings: false });
	};

	prefs.setTheme('light');
	await gate;

	assert.equal(seen.url, `${API_BASE}/api/v1/ui-prefs`);
	assert.equal(seen.method, 'PUT');
	assert.equal(seen.headers.get('content-type'), 'application/json');
	assert.deepEqual(body, { theme: 'light' });
	assert.equal(prefs.uiPrefs.theme, 'light');
});

test('hydrateConfirmPrefsFromDisk GETs ui-prefs and applies fields', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({
			theme: 'light',
			hide_todo_settings: true,
			confirm: { relocate: true },
			auto_sync: { rekordbox: true, djay: false, open_dj: true }
		});
	};

	// Reset local fields that hydrate should overwrite.
	prefs.uiPrefs.theme = 'dark';
	prefs.uiPrefs.hide_todo_settings = false;
	prefs.uiPrefs.confirm = {};

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.equal(seen.url, `${API_BASE}/api/v1/ui-prefs`);
	assert.equal(seen.method, 'GET');
	assert.equal(prefs.uiPrefs.theme, 'light');
	assert.equal(prefs.uiPrefs.hide_todo_settings, true);
	assert.equal(prefs.uiPrefs.confirm.relocate, true);
});

test('hydrateConfirmPrefsFromDisk resolves quietly on non-2xx', async () => {
	prefs.uiPrefs.theme = 'dark';
	prefs.uiPrefs.hide_todo_settings = false;
	const beforeConfirm = { ...prefs.uiPrefs.confirm };

	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: 'nope' }), {
			status: 503,
			statusText: 'Service Unavailable',
			headers: { 'content-type': 'application/json' }
		});

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.equal(prefs.uiPrefs.theme, 'dark');
	assert.equal(prefs.uiPrefs.hide_todo_settings, false);
	assert.deepEqual(prefs.uiPrefs.confirm, beforeConfirm);
});

test('hydrateConfirmPrefsFromDisk resolves quietly when the daemon is unreachable', async () => {
	prefs.uiPrefs.theme = 'dark';
	prefs.uiPrefs.hide_todo_settings = false;

	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.equal(prefs.uiPrefs.theme, 'dark');
	assert.equal(prefs.uiPrefs.hide_todo_settings, false);
});

test('playlist-tree width defaults, persists, and clamps at documented bounds', async () => {
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		assert.equal(isolated.uiPrefs.playlist_tree_width, 300);
		isolated.setPlaylistTreeWidth(999);
		assert.equal(isolated.uiPrefs.playlist_tree_width, 520);
		assert.equal(JSON.parse(storage.values.get('mdt.rb.ui-prefs.v1')).playlist_tree_width, 520);
		isolated.setPlaylistTreeWidth(1);
		assert.equal(isolated.uiPrefs.playlist_tree_width, 220);
	} finally {
		storage.restore();
	}
});

test('playlist-tree width rejects malformed stored values', async () => {
	const storage = installPrefsStorage(JSON.stringify({ hide_broken_links: false, playlist_tree_width: 521 }));
	try {
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE }),
			/playlist_tree_width must be an integer from 220 through 520/
		);
	} finally {
		storage.restore();
	}
});
