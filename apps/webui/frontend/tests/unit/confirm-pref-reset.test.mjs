// PR #4014 (Sol P1 r4167041271 on prefs.svelte.ts clearConfirmPref): resetting
// a remembered confirm choice to "Ask each time" must use the verified disk
// write, drop the live choice only after the delete lands, and surface a
// failure to the user instead of letting the next hydration restore Add/Move.
// Both directions are pinned: a failed reset keeps the choice and reports it (an
// error toast in the app), and a successful reset clears it and reports nothing.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://confirm-reset.example.test';
const RECORDING_SINK = fileURLToPath(new URL('./fake-setting-save-errors.mjs', import.meta.url));

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
		alias: { '$lib/settings/setting-save-errors': RECORDING_SINK }
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	prefs.uiPrefs.confirm = { playlist_drop_mode: 'move', delete_playlist: false };
	globalThis.__recordedSettingSaveErrors = [];
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

test('Settings "Ask each time" reports the failure when the reset fails', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ detail: { code: 'WRITE_FAILED', message: 'disk full' } }, 500);
	apply.applySettingChange('confirm.playlist_drop_mode', 'move');
	await settle();
	globalThis.__recordedSettingSaveErrors = [];
	globalThis.fetch = async () =>
		jsonResponse({ detail: { code: 'WRITE_FAILED', message: 'disk full' } }, 500);

	apply.applySettingChange('confirm.playlist_drop_mode', 'ask');
	await settle();

	assert.equal(globalThis.__recordedSettingSaveErrors.length, 1);
	assert.match(globalThis.__recordedSettingSaveErrors[0].message, /playlist drop.*disk full/);
	assert.equal(apply.readSettingValue('confirm.playlist_drop_mode'), 'move');
});

test('Settings "Ask each time" clears the choice and stays quiet when the reset lands', async () => {
	globalThis.fetch = async () => jsonResponse({ confirm: { playlist_drop_mode: 'move' } });
	apply.applySettingChange('confirm.playlist_drop_mode', 'move');
	await settle();
	globalThis.__recordedSettingSaveErrors = [];
	globalThis.fetch = async () => jsonResponse({ confirm: {} });

	apply.applySettingChange('confirm.playlist_drop_mode', 'ask');
	await settle();

	assert.deepEqual(globalThis.__recordedSettingSaveErrors, []);
	assert.equal(apply.readSettingValue('confirm.playlist_drop_mode'), 'ask');
});

test('the app shell routes reported settings save failures to an error toast', async () => {
	// Structural half: app-init.ts installs the sink at boot (loading app-init
	// would start every page instrument). Behavioral half: the real sink module
	// forwards to whatever is installed, and logs (never drops) before that.
	const appInit = readFileSync(new URL('../../src/lib/rb/app-init.ts', import.meta.url), 'utf8');
	assert.match(
		appInit,
		/installSettingSaveErrorSink\(\(message, cause\) => pushToast\(message, 'error', undefined, cause\)\)/
	);

	const sinkModule = await loadTypeScriptModule('src/lib/settings/setting-save-errors.ts');
	const logged = [];
	const originalError = console.error;
	console.error = (...args) => logged.push(args);
	try {
		sinkModule.reportSettingSaveError('before install', 'cause-1');
	} finally {
		console.error = originalError;
	}
	assert.equal(logged.length, 1);
	assert.match(String(logged[0][0]), /before install/);

	const seen = [];
	sinkModule.installSettingSaveErrorSink((message, cause) => seen.push({ message, cause }));
	sinkModule.reportSettingSaveError('after install', 'cause-2');
	assert.deepEqual(seen, [{ message: 'after install', cause: 'cause-2' }]);
});

// Sol (review bodies, three rounds) on prefs-hydrate.ts _hydrateConfirmFromDisk:
// a successful disk read is authoritative for the known confirm keys, so a
// choice another profile reset to "ask" is removed here too. The overshoot
// control: a local choice disk has not acknowledged yet must survive.
function routeFetch({ get, put }) {
	return async (request) => (request.method === 'GET' ? get(request) : put(request));
}

test('hydration removes a remembered drop mode the disk map no longer has', async () => {
	globalThis.fetch = routeFetch({
		get: () => jsonResponse({ confirm: { delete_playlist: false } }),
		put: () => jsonResponse({})
	});

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.equal(prefs.uiPrefs.confirm.playlist_drop_mode, undefined);
	assert.equal(prefs.uiPrefs.confirm.delete_playlist, false, 'a key present on disk still hydrates');
});

test('hydration keeps a choice whose PUT is still in flight, then honors disk once it lands', async () => {
	prefs.uiPrefs.confirm = {};
	let releasePut;
	const putGate = new Promise((resolve) => {
		releasePut = resolve;
	});
	globalThis.fetch = routeFetch({
		get: () => jsonResponse({ confirm: {} }),
		put: async () => {
			await putGate;
			return jsonResponse({});
		}
	});

	prefs.setConfirmPref('playlist_drop_mode', 'move');
	await prefs.hydrateConfirmPrefsFromDisk();
	assert.equal(prefs.uiPrefs.confirm.playlist_drop_mode, 'move', 'an unacknowledged choice must survive');

	releasePut();
	await settle();
	await prefs.hydrateConfirmPrefsFromDisk();
	assert.equal(prefs.uiPrefs.confirm.playlist_drop_mode, undefined, 'once saved, disk is authoritative again');
});

test('a failed confirm PUT keeps the choice through hydration and the next write resends it', async () => {
	prefs.uiPrefs.confirm = {};
	const originalWarn = console.warn;
	console.warn = () => {};
	try {
		globalThis.fetch = routeFetch({
			get: () => jsonResponse({ confirm: {} }),
			put: async () => {
				throw new TypeError('Failed to fetch');
			}
		});
		prefs.setConfirmPref('playlist_drop_mode', 'add');
		await settle();
		await prefs.hydrateConfirmPrefsFromDisk();
		assert.equal(prefs.uiPrefs.confirm.playlist_drop_mode, 'add');

		const bodies = [];
		globalThis.fetch = routeFetch({
			get: () => jsonResponse({ confirm: { playlist_drop_mode: 'add', delete_playlist: false } }),
			put: async (request) => {
				bodies.push(await request.clone().json());
				return jsonResponse({});
			}
		});
		prefs.setConfirmPref('delete_playlist', false);
		await settle();
		assert.deepEqual(bodies, [{ confirm: { playlist_drop_mode: 'add', delete_playlist: false } }]);
	} finally {
		console.warn = originalWarn;
	}
});

test('unknown confirm keys behave as before: kept locally, applied from disk', async () => {
	prefs.uiPrefs.confirm = { legacy_local_only: true };
	globalThis.fetch = routeFetch({
		get: () => jsonResponse({ confirm: { future_disk_key: false } }),
		put: () => jsonResponse({})
	});

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.equal(prefs.uiPrefs.confirm.legacy_local_only, true);
	assert.equal(prefs.uiPrefs.confirm.future_disk_key, false);
});
