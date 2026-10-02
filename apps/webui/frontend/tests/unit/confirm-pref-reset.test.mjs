// PR #4014 (Sol P1 r4167041271 on prefs.svelte.ts clearConfirmPref): resetting
// a remembered confirm choice to "Ask each time" must use the verified disk
// write, drop the live choice only after the delete lands, and surface a
// failure to the user instead of letting the next hydration restore Add/Move.
// Both directions are pinned: a failed reset keeps the choice and toasts, and
// a successful reset clears it without a toast.
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://confirm-reset.example.test';
const RECORDING_STORES = fileURLToPath(new URL('./fake-stores-recording.mjs', import.meta.url));

let prefs;
let apply;
let originalFetch;

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

/** Let the queued write chain and its catch handlers run to completion. */
async function settle() {
	for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

before(async () => {
	prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	apply = await loadTypeScriptModule('src/lib/settings/apply.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/stores.svelte': RECORDING_STORES }
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	prefs.uiPrefs.confirm = { playlist_drop_mode: 'move', delete_playlist: false };
	globalThis.__recordedToasts = [];
});

test('a 2xx reset deletes the disk key, then clears the live choice', async () => {
	let body;
	let liveDuringPut;
	globalThis.fetch = async (request) => {
		body = await request.clone().json();
		liveDuringPut = prefs.uiPrefs.confirm.playlist_drop_mode;
		return jsonResponse({ confirm: {} });
	};

	await prefs.clearConfirmPref('playlist_drop_mode');

	assert.deepEqual(body, { confirm: { playlist_drop_mode: null } });
	assert.equal(liveDuringPut, 'move', 'the reset must not commit before the PUT lands');
	assert.equal(prefs.uiPrefs.confirm.playlist_drop_mode, undefined);
	assert.equal(prefs.uiPrefs.confirm.delete_playlist, false);
});

test('an HTTP error rejects and keeps the remembered choice', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ detail: { code: 'WRITE_FAILED', message: 'disk full' } }, 500);

	await assert.rejects(prefs.clearConfirmPref('playlist_drop_mode'), /disk full/);
	assert.equal(prefs.uiPrefs.confirm.playlist_drop_mode, 'move');
});

test('a transport failure rejects and keeps the remembered choice', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('Failed to fetch');
	};

	await assert.rejects(prefs.clearConfirmPref('playlist_drop_mode'), /Failed to fetch/);
	assert.equal(prefs.uiPrefs.confirm.playlist_drop_mode, 'move');
});

test('Settings "Ask each time" shows an error toast when the reset fails', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ detail: { code: 'WRITE_FAILED', message: 'disk full' } }, 500);
	apply.applySettingChange('confirm.playlist_drop_mode', 'move');
	await settle();
	globalThis.__recordedToasts = [];
	globalThis.fetch = async () =>
		jsonResponse({ detail: { code: 'WRITE_FAILED', message: 'disk full' } }, 500);

	apply.applySettingChange('confirm.playlist_drop_mode', 'ask');
	await settle();

	assert.equal(globalThis.__recordedToasts.length, 1);
	assert.equal(globalThis.__recordedToasts[0].kind, 'error');
	assert.match(globalThis.__recordedToasts[0].message, /playlist drop choice.*disk full/);
	assert.equal(apply.readSettingValue('confirm.playlist_drop_mode'), 'move');
});

test('Settings "Ask each time" clears the choice and stays quiet when the reset lands', async () => {
	globalThis.fetch = async () => jsonResponse({ confirm: { playlist_drop_mode: 'move' } });
	apply.applySettingChange('confirm.playlist_drop_mode', 'move');
	await settle();
	globalThis.__recordedToasts = [];
	globalThis.fetch = async () => jsonResponse({ confirm: {} });

	apply.applySettingChange('confirm.playlist_drop_mode', 'ask');
	await settle();

	assert.deepEqual(globalThis.__recordedToasts, []);
	assert.equal(apply.readSettingValue('confirm.playlist_drop_mode'), 'ask');
});
