/**
 * The first-run native folder picker's outcomes (src/lib/setup/folder-picker.ts, #13152).
 *
 * - if a browser tab (no desktop bridge) reaches the picker then the click
 *   throws or clears the typed path -> broken
 * - if a rejected native dialog or a failed plugin load writes to the row then
 *   the path the user typed is lost -> broken
 * - if a cancelled dialog is read as a path then the row is overwritten with
 *   nothing -> broken
 * - if a returned folder does not reach the wizard then choosing is not the
 *   whole gesture (SETUP-29) -> broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const TYPED = '/Users/dev/Typed Music';

let mod;
let modWithoutDialogPlugin;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/setup/folder-picker.ts');
	modWithoutDialogPlugin = await loadTypeScriptModule('src/lib/setup/folder-picker.ts', {
		failImports: [/^@tauri-apps\/plugin-dialog$/]
	});
});

/** A stand-in for the wizard's rows: one row holding a typed path. */
function typedRows() {
	const rows = [{ id: 'row-1', path: TYPED, scan: null }];
	const chosen = [];
	const choose = (id, path) => {
		chosen.push([id, path]);
		for (const row of rows) if (row.id === id) row.path = path;
	};
	return { rows, chosen, choose };
}

/** An Electron preload bridge whose picker answers with `pickFolder`. */
function electronScope(pickFolder) {
	const titles = [];
	const scope = {
		opendjShell: {
			kind: 'electron',
			version: 1,
			pickFolder: async (options) => {
				titles.push(options);
				return pickFolder();
			}
		}
	};
	return { scope, titles };
}

test('with no desktop bridge the picker does nothing and keeps the typed path', async () => {
	// A browser tab and a lookalike global are both "no bridge".
	for (const scope of [{}, { opendjShell: { kind: 'web' } }, { __TAURI_INTERNALS__: null }]) {
		const { rows, chosen, choose } = typedRows();
		assert.equal(await mod.pickFolderIntoRow('row-1', choose, scope), 'unavailable');
		assert.deepEqual(chosen, []);
		assert.equal(rows[0].path, TYPED);
	}
});

test('a rejected Electron dialog keeps the typed path and does not throw', async () => {
	const { rows, chosen, choose } = typedRows();
	const { scope } = electronScope(() => {
		throw new Error('dialog refused');
	});
	assert.equal(await mod.pickFolderIntoRow('row-1', choose, scope), 'failed');
	assert.deepEqual(chosen, []);
	assert.equal(rows[0].path, TYPED);
});

test('a Tauri dialog plugin that fails to load keeps the typed path', async () => {
	const { rows, chosen, choose } = typedRows();
	const scope = { __TAURI_INTERNALS__: {} };
	assert.equal(await modWithoutDialogPlugin.pickFolderIntoRow('row-1', choose, scope), 'failed');
	assert.deepEqual(chosen, []);
	assert.equal(rows[0].path, TYPED);
});

test('a cancelled dialog is not a path and keeps the typed path', async () => {
	const { rows, chosen, choose } = typedRows();
	const { scope } = electronScope(() => null);
	assert.equal(await mod.pickFolderIntoRow('row-1', choose, scope), 'cancelled');
	assert.deepEqual(chosen, []);
	assert.equal(rows[0].path, TYPED);
});

test('a returned folder path is handed to the wizard for that row', async () => {
	const { rows, chosen, choose } = typedRows();
	const { scope, titles } = electronScope(() => '/Users/dev/Music');
	assert.equal(await mod.pickFolderIntoRow('row-1', choose, scope), 'chosen');
	assert.deepEqual(titles, [{ title: 'Choose a folder' }]);
	assert.deepEqual(chosen, [['row-1', '/Users/dev/Music']]);
	assert.equal(rows[0].path, '/Users/dev/Music');
});
