import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://show-stems-ipc.example.test';

function installPrefsStorage(raw = null) {
	const store = new Map();
	if (raw !== null) store.set('mdt.rb.ui-prefs.v1', raw);
	const storage = {
		getItem(key) {
			return store.has(key) ? store.get(key) : null;
		},
		setItem(key, value) {
			store.set(key, value);
		},
		removeItem(key) {
			store.delete(key);
		}
	};
	const original = globalThis.localStorage;
	globalThis.localStorage = storage;
	return {
		read() {
			return store.get('mdt.rb.ui-prefs.v1') ?? null;
		},
		restore() {
			if (original === undefined) delete globalThis.localStorage;
			else globalThis.localStorage = original;
		},
		storage
	};
}

let ipc;

before(async () => {
	await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', { viteApiBase: API_BASE });
});

test('show_stems command toggles pref and query().ui.show_stems', async () => {
	const storage = installPrefsStorage();
	globalThis.window = { localStorage: storage.storage };
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		assert.equal(ipc.queryPerformanceState().ui.show_stems, false);
		await window.musicDjToolsPerformance.dispatch({ type: 'show_stems', enabled: true });
		assert.equal(ipc.queryPerformanceState().ui.show_stems, true);
		const saved = JSON.parse(storage.read());
		assert.equal(saved.show_stems, true);
		await window.musicDjToolsPerformance.dispatch({ type: 'show_stems', enabled: false });
		assert.equal(ipc.queryPerformanceState().ui.show_stems, false);
	} finally {
		uninstall();
		delete globalThis.window;
		storage.restore();
	}
});

test('show_stems parses through parsePerformanceCommandForTest', () => {
	const cmd = ipc.parsePerformanceCommandForTest({ type: 'show_stems', enabled: true });
	assert.deepEqual(cmd, { type: 'show_stems', enabled: true });
});
