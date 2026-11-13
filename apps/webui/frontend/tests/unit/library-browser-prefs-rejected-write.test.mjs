// [if] a library-browser pref setter's PUT is refused [then] the DJ is told it was not saved,
// the local value stands and disk is untouched; [if] a later PUT succeeds [then] it lands on
// disk; [if] the page reloads [then] it hydrates from the last value disk accepted (issue
// #13208, spec 7f98c552 section 2).
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const API_BASE = 'https://library-browser-prefs-rejected.example.test';
/** Records what the app would show as an error toast (app-init installs the toast sink). */
const RECORDING_SINK = fileURLToPath(new URL('./fake-setting-save-errors.mjs', import.meta.url));

/** Plain library-browser setters (localStorage authoritative): [setter, key, before, after]. */
const LIBRARY_BROWSER_SETTERS = [
	['setHideBrokenLinks', 'hide_broken_links', false, true],
	['setLibraryDensity', 'library_density', 'compact', 'cosy'],
	['setNextOnlyFilter', 'next_only_filter', false, true],
	['setRemixesFilter', 'remixes_filter', false, true],
	['setVocalsFilter', 'vocals_filter', false, true],
	['setAvailableOfflineFilter', 'available_offline_filter', false, true]
];

/** The two ways a PUT can fail: the daemon answers non-2xx, or the request never lands. */
const FAILURE_MODES = ['status', 'network'];

const realFetch = globalThis.fetch;

/**
 * In-memory fake of GET/PUT /api/v1/ui-prefs (a double for the daemon, not the
 * subject): PUT merges its body into `disk` unless `refuse` is set, GET returns
 * `disk`. Synthesized, not recorded: the GET always carries the three fields the
 * hydrator's contract requires (key_notation, library_hidden_columns,
 * library_key_filter), as the real UiPrefsOut does.
 */
function installFakeDaemon(initialDisk = {}) {
	const daemon = {
		disk: {
			key_notation: 'camelot',
			library_hidden_columns: [],
			library_key_filter: [],
			...initialDisk
		},
		refuse: null,
		attempts: [],
		accepted: []
	};
	globalThis.fetch = async (input, init) => {
		const request = new Request(input, init);
		const path = new URL(request.url).pathname;
		assert.equal(path, '/api/v1/ui-prefs', `unexpected request to ${path}`);
		if (request.method === 'GET') {
			return _json(200, daemon.disk);
		}
		assert.equal(request.method, 'PUT');
		const body = await request.clone().json();
		daemon.attempts.push(body);
		if (daemon.refuse === 'network') throw new TypeError('fetch failed');
		if (daemon.refuse === 'status') return _json(503, { detail: 'ui-prefs store unavailable' });
		daemon.accepted.push(body);
		daemon.disk = { ...daemon.disk, ...body };
		return _json(200, daemon.disk);
	};
	return daemon;
}

function _json(status, body) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

function installBrowser(store = new Map()) {
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => void store.set(k, String(v)),
			removeItem: (k) => void store.delete(k)
		}
	};
	globalThis.document = { documentElement: { dataset: {}, style: {} } };
	return store;
}

const stored = (store) => JSON.parse(store.get(STORAGE_KEY));

const loadPrefs = () =>
	loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/settings/setting-save-errors': RECORDING_SINK }
	});

const reported = () => globalThis.__recordedSettingSaveErrors;

/** Let the shared disk write chain drain its queued PUTs. */
async function settle() {
	for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
	globalThis.__recordedSettingSaveErrors = [];
});

afterEach(() => {
	globalThis.fetch = realFetch;
	delete globalThis.__recordedSettingSaveErrors;
	delete globalThis.window;
	delete globalThis.document;
});

test('a refused PUT is reported to the DJ, keeps the new value live, and disk keeps the old one', async () => {
	// Every plain library-browser setter, under both failure modes.
	for (const mode of FAILURE_MODES) {
		for (const [setter, key, before, after] of LIBRARY_BROWSER_SETTERS) {
			const label = `${setter} (${mode} failure)`;
			globalThis.__recordedSettingSaveErrors = [];
			const store = installBrowser();
			const daemon = installFakeDaemon({ [key]: before });
			const prefs = await loadPrefs();
			daemon.refuse = mode;
			// The setter does not throw: the failure goes to the save-error sink instead.
			assert.doesNotThrow(() => prefs[setter](after), label);
			await settle();
			assert.deepEqual(daemon.attempts, [{ [key]: after }], `${label}: the PUT was attempted`);
			assert.deepEqual(daemon.accepted, [], `${label}: nothing was accepted`);
			assert.equal(reported().length, 1, `${label}: the refused save was not shown to the DJ`);
			assert.match(reported()[0].message, /not saved to disk/, label);
			assert.ok(reported()[0].cause instanceof Error, `${label}: the cause was dropped`);
			assert.equal(daemon.disk[key], before, `${label}: disk must keep the last accepted value`);
			assert.equal(prefs.uiPrefs[key], after, `${label}: live value reverted`);
			assert.equal(stored(store)[key], after, `${label}: localStorage reverted`);
		}
	}
});

test('a refused PUT does not wedge the queue: the next PUT lands, and only its own key', async () => {
	// The failed key is not resent by a later, unrelated write.
	for (const mode of FAILURE_MODES) {
		const store = installBrowser();
		const daemon = installFakeDaemon({ hide_broken_links: false, library_density: 'compact' });
		const prefs = await loadPrefs();
		daemon.refuse = mode;
		prefs.setHideBrokenLinks(true);
		await settle();
		daemon.refuse = null;
		prefs.setLibraryDensity('cosy');
		await settle();
		assert.deepEqual(daemon.accepted, [{ library_density: 'cosy' }], `${mode}: later PUT`);
		assert.equal(daemon.disk.library_density, 'cosy', `${mode}: later PUT never reached disk`);
		assert.equal(daemon.disk.hide_broken_links, false, `${mode}: refused value appeared on disk`);
		assert.equal(prefs.uiPrefs.hide_broken_links, true, `${mode}: refused value lost locally`);
		assert.equal(stored(store).hide_broken_links, true);
		assert.equal(stored(store).library_density, 'cosy');
		assert.equal(reported().length, 1, `${mode}: only the refused save is reported`);
		globalThis.__recordedSettingSaveErrors = [];
	}
});

test('a reload after a refused PUT hydrates from the last value disk accepted', async () => {
	// Disk wins on boot: the unsaved local value shows until the GET lands, then
	// the page and localStorage follow disk.
	for (const mode of FAILURE_MODES) {
		for (const [setter, key, before, after] of LIBRARY_BROWSER_SETTERS) {
			const label = `${setter} (${mode} failure)`;
			const store = installBrowser();
			const daemon = installFakeDaemon({ [key]: before });
			const first = await loadPrefs();
			daemon.refuse = mode;
			first[setter](after);
			await settle();
			assert.equal(stored(store)[key], after, `${label}: not persisted locally`);

			daemon.refuse = null;
			installBrowser(store);
			const reloaded = await loadPrefs();
			assert.equal(reloaded.uiPrefs[key], after, `${label}: boot did not read localStorage`);
			await reloaded.hydrateConfirmPrefsFromDisk();
			await settle();
			assert.equal(reloaded.uiPrefs[key], before, `${label}: reload ignored the disk value`);
			assert.equal(stored(store)[key], before, `${label}: localStorage not realigned with disk`);
			assert.deepEqual(
				daemon.accepted.filter((body) => key in body),
				[],
				`${label}: reload resent the refused value`
			);
		}
	}
});

test('setLibraryKeyFilter surfaces a refused PUT to its caller and falls back to the saved disk value', async () => {
	// The verified key-filter write rejects, then re-reads disk so the table shows what is saved.
	for (const mode of FAILURE_MODES) {
		const store = installBrowser();
		const daemon = installFakeDaemon({ library_key_filter: ['8A'] });
		const prefs = await loadPrefs();
		await prefs.hydrateConfirmPrefsFromDisk();
		assert.deepEqual(prefs.uiPrefs.library_key_filter, ['8A']);
		daemon.refuse = mode;
		const write = prefs.setLibraryKeyFilter(['9A']);
		assert.deepEqual(prefs.uiPrefs.library_key_filter, ['9A'], `${mode}: not applied at once`);
		await assert.rejects(write, `${mode}: the refused write was swallowed`);
		await settle();
		assert.deepEqual(
			daemon.attempts,
			[{ library_key_filter: ['9A'] }],
			`${mode}: no PUT attempted`
		);
		assert.deepEqual(daemon.disk.library_key_filter, ['8A'], `${mode}: refused value on disk`);
		assert.deepEqual(prefs.uiPrefs.library_key_filter, ['8A'], `${mode}: not re-read from disk`);
		assert.deepEqual(
			stored(store).library_key_filter,
			['8A'],
			`${mode}: localStorage not realigned`
		);
		assert.equal(prefs.libraryKeyFilterSync.error, null, `${mode}: the disk re-read itself failed`);

		daemon.refuse = null;
		await prefs.setLibraryKeyFilter(['9A']);
		await settle();
		assert.deepEqual(daemon.disk.library_key_filter, ['9A'], `${mode}: later PUT never landed`);
		assert.deepEqual(prefs.uiPrefs.library_key_filter, ['9A'], mode);
	}
});
