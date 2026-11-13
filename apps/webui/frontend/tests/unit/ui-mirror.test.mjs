/**
 * Issue #13281 / AGENT-02: browser search, sort, and selected_row reach the UI
 * mirror without DOM scraping. Shipped with #3195 (PR #3212); this file guards
 * the ui-mirror projection layer by calling the real `buildUiMirror`.
 *
 * Regression lines:
 *   [if] buildUiMirror hardcodes null browser fields [then] agents cannot read navigation state - broken
 *   [if] search/sort/selected_row omit state.browser [then] IPC wiring is bypassed at publish - broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

globalThis.location ??= { search: '' };

const storage = {
	getItem: () => null,
	setItem: () => {},
	removeItem: () => {},
	clear: () => {}
};
globalThis.localStorage = storage;
globalThis.window = { localStorage: storage };

let bundle;

before(async () => {
	bundle = await loadTypeScriptModule('tests/unit/fixtures/ui-mirror-browser-entry.ts', {
		viteApiBase: 'https://ui-mirror-browser.example.test'
	});
	bundle.installPerformanceBrowserIpc();
});

function withDocumentStub(run) {
	const prior = globalThis.document;
	globalThis.document = {
		querySelectorAll: () => [],
		visibilityState: 'visible'
	};
	try {
		return run();
	} finally {
		if (prior === undefined) delete globalThis.document;
		else globalThis.document = prior;
	}
}

function browserBlock() {
	const published = withDocumentStub(() => bundle.buildUiMirror());
	const block = published.browser;
	assert.ok(block && typeof block === 'object', 'control: published mirror has browser block');
	return /** @type {{ search: unknown; sort: unknown; selected_row: unknown; playlist: unknown; load: unknown }} */ (
		block
	);
}

test('[if] no browser adapter is registered [then] buildUiMirror publishes explicit null search sort selected_row', () => {
	const block = browserBlock();
	assert.equal(block.search, null);
	assert.equal(block.sort, null);
	assert.equal(block.selected_row, null);
	assert.ok('search' in block);
	assert.ok('sort' in block);
	assert.ok('selected_row' in block);
	assert.doesNotThrow(() => structuredClone(block));
});

test('[if] the browser adapter returns search sort selected_row load [then] buildUiMirror matches the adapter snapshot', () => {
	const unregister = bundle.registerPerformanceBrowserAdapter({
		selectPlaylist: async () => {},
		readSnapshot: () => ({
			search: 'techno',
			sort: { key: 'artist', direction: 'desc' },
			selected_row: 'stable-id-42',
			load: { complete: true, rows_loaded: 120 }
		})
	});
	try {
		const block = browserBlock();
		const ipcBrowser = bundle.queryPerformanceState().browser;
		assert.equal(block.search, 'techno');
		assert.deepEqual(block.sort, { key: 'artist', direction: 'desc' });
		assert.equal(block.selected_row, 'stable-id-42');
		assert.deepEqual(block.load, { complete: true, rows_loaded: 120 });
		assert.equal(block.playlist, ipcBrowser.active_playlist);
		assert.doesNotThrow(() => structuredClone(block));
	} finally {
		unregister();
	}
});

test('[if] the adapter returns only sort [then] buildUiMirror still publishes null search and selected_row', () => {
	const unregister = bundle.registerPerformanceBrowserAdapter({
		selectPlaylist: async () => {},
		readSnapshot: () => ({
			search: null,
			sort: { key: 'bpm', direction: 'desc' },
			selected_row: null,
			load: null
		})
	});
	try {
		const block = browserBlock();
		assert.equal(block.search, null);
		assert.deepEqual(block.sort, { key: 'bpm', direction: 'desc' });
		assert.equal(block.selected_row, null);
	} finally {
		unregister();
	}
});
