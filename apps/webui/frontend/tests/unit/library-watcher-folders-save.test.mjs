// LIBM-129 / PR #4014 (Sol P1 on WatcherFoldersEditor.svelte): saving watcher
// folders must resolve only after the disk PUT succeeds, and reject (leaving
// the live prefs untouched) on an HTTP error or a transport failure, so the
// editor never shows "Saved" for a write that did not land.
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://watcher-save.example.test';

let prefs;
let originalFetch;

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
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

beforeEach(() => {
	prefs.uiPrefs.library_watcher_folders = ['/Users/dev/old'];
});

test('setLibraryWatcherFolders resolves after a 2xx PUT and applies the folders', async () => {
	let body;
	globalThis.fetch = async (request) => {
		body = await request.clone().json();
		return jsonResponse({ library_watcher_folders: ['/Users/dev/watch-a'] });
	};

	await prefs.setLibraryWatcherFolders(['/Users/dev/watch-a']);

	assert.deepEqual(body, { library_watcher_folders: ['/Users/dev/watch-a'] });
	assert.deepEqual(prefs.uiPrefs.library_watcher_folders, ['/Users/dev/watch-a']);
});

test('setLibraryWatcherFolders rejects on an HTTP error and keeps the old folders', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ detail: { code: 'WRITE_FAILED', message: 'disk full' } }, 500);

	await assert.rejects(prefs.setLibraryWatcherFolders(['/Users/dev/watch-a']), /disk full/);
	assert.deepEqual(prefs.uiPrefs.library_watcher_folders, ['/Users/dev/old']);
});

test('setLibraryWatcherFolders rejects on a transport failure and keeps the old folders', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('Failed to fetch');
	};

	await assert.rejects(prefs.setLibraryWatcherFolders(['/Users/dev/watch-a']), /Failed to fetch/);
	assert.deepEqual(prefs.uiPrefs.library_watcher_folders, ['/Users/dev/old']);
});

test('a failed verified save does not wedge the shared disk write chain', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('Failed to fetch');
	};
	await assert.rejects(prefs.setLibraryWatcherFolders(['/Users/dev/watch-a']));

	let release;
	const gate = new Promise((resolve) => {
		release = resolve;
	});
	let body;
	globalThis.fetch = async (request) => {
		body = await request.clone().json();
		release();
		return jsonResponse({ theme: 'light' });
	};
	prefs.setTheme('light');
	await gate;
	assert.equal(body.theme, 'light');
});
