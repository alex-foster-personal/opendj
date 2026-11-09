import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('LIBUX-02 (#2166): manual expand survives layout-induced re-crossing', async () => {
	globalThis.window = {};
	const lib = await loadTypeScriptModule('src/lib/rb/library-panels.svelte.ts');
	try {
		lib.noteVisibleLibraryRowCount(5, 'p1');
		assert.equal(lib.isLibraryPanelsCollapsed(), true);
		lib.noteVisibleLibraryRowCount(14, 'p1');
		assert.equal(lib.isLibraryPanelsCollapsed(), true, 'still collapsed above floor');
		lib.setLibraryPanelsCollapsed(false);
		assert.equal(lib.isLibraryPanelsCollapsed(), false);
		lib.noteVisibleLibraryRowCount(5, 'p1');
		assert.equal(lib.isLibraryPanelsCollapsed(), false, 'manual expand holds through remount dip');
		lib.noteVisibleLibraryRowCount(14, 'p1');
		assert.equal(lib.isLibraryPanelsCollapsed(), false);
		lib.noteVisibleLibraryRowCount(5, 'p1');
		assert.equal(lib.isLibraryPanelsCollapsed(), false);
	} finally {
		delete globalThis.window;
	}
});

test('LIBUX-02: edge latch without manual expand', async () => {
	globalThis.window = {};
	const lib = await loadTypeScriptModule('src/lib/rb/library-panels.svelte.ts');
	try {
		lib.noteVisibleLibraryRowCount(5, 'p1');
		assert.equal(lib.isLibraryPanelsCollapsed(), true);
		lib.noteVisibleLibraryRowCount(4, 'p1');
		assert.equal(lib.isLibraryPanelsCollapsed(), true, 'repeat under floor does not re-force');
		lib.setLibraryPanelsCollapsed(false);
		lib.noteVisibleLibraryRowCount(4, 'p1');
		assert.equal(lib.isLibraryPanelsCollapsed(), false);
	} finally {
		delete globalThis.window;
	}
});

test('LIBUX-02: pane key change starts a fresh auto-collapse pass', async () => {
	globalThis.window = {};
	const lib = await loadTypeScriptModule('src/lib/rb/library-panels.svelte.ts');
	try {
		lib.noteVisibleLibraryRowCount(5, 'p1');
		lib.setLibraryPanelsCollapsed(false);
		lib.noteVisibleLibraryRowCount(5, 'p2');
		assert.equal(lib.isLibraryPanelsCollapsed(), true);
	} finally {
		delete globalThis.window;
	}
});

test('LIBUX-02: explicit collapse clears manual override', async () => {
	globalThis.window = {};
	const lib = await loadTypeScriptModule('src/lib/rb/library-panels.svelte.ts');
	try {
		lib.noteVisibleLibraryRowCount(5, 'p1');
		lib.setLibraryPanelsCollapsed(false);
		lib.setLibraryPanelsCollapsed(true);
		lib.setLibraryPanelsCollapsed(false);
		lib.noteVisibleLibraryRowCount(5, 'p1');
		assert.equal(lib.isLibraryPanelsCollapsed(), false);
	} finally {
		delete globalThis.window;
	}
});

test('LIBUX-02 (#2166): intentionally blank panes settle to zero rows', async () => {
	globalThis.window = {};
	const lib = await loadTypeScriptModule('src/lib/rb/library-panels.svelte.ts');
	try {
		const blank = { playlist_id: null, loading: false, has_settled_result: false };
		assert.equal(lib.settledLibraryRowCount(blank, true, 0), 0);
		lib.noteVisibleLibraryRowCount(0, null);
		assert.equal(lib.isLibraryPanelsCollapsed(), true);
		assert.equal(lib.settledLibraryRowCount(blank, false, 0), null);
		assert.equal(lib.settledLibraryRowCount({ ...blank, loading: true }, true, 0), null);
		assert.equal(
			lib.settledLibraryRowCount(
				{ playlist_id: 'pl-1', loading: false, has_settled_result: true },
				true,
				12
			),
			12
		);
		assert.equal(
			lib.settledLibraryRowCount(
				{ playlist_id: 'pl-1', loading: false, has_settled_result: false },
				true,
				12
			),
			null
		);
	} finally {
		delete globalThis.window;
	}
});

test('LIBUX-02: bad inputs throw TypeError', async () => {
	globalThis.window = {};
	const lib = await loadTypeScriptModule('src/lib/rb/library-panels.svelte.ts');
	try {
		assert.throws(() => lib.setLibraryPanelsCollapsed('yes'), /boolean/);
		assert.throws(() => lib.noteVisibleLibraryRowCount(-1, 'p1'), /non-negative/);
		assert.throws(() => lib.noteVisibleLibraryRowCount(5, 1), /paneKey/);
	} finally {
		delete globalThis.window;
	}
});
