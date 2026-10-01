import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Track context-menu Bulk edit / Find/replace / My Tag editor (LIBM-62/66/68).
// Regression lines:
// - if any of the three items lacks `run` then ContextMenu disables it with PARITY-TODO
// - if `run` calls the opener with the wrong kind then the toolbar modal does not open
// - if empty selection still uses the PARITY-TODO title then the user is not told why

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const EMPTY_TITLE = 'select at least one track first';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/track-edit-menu.ts');
});

// REQ: LIBM-68
test('Bulk edit, Find/replace and My Tag editor carry run and invoke the opener with the matching kind', () => {
	const kinds = [];
	const items = mod.trackEditMenuItems(3, (kind) => kinds.push(kind));
	assert.deepEqual(
		items.map((item) => item.id),
		['bulk-edit', 'find-replace', 'mytag']
	);
	for (const item of items) {
		assert.equal(typeof item.run, 'function', `${item.id} must carry run`);
	}
	assert.equal(items[0].label, 'Bulk edit (3)');
	for (const item of items) item.run();
	assert.deepEqual(kinds, ['bulk-edit', 'find-replace', 'mytag']);
});

test('Bulk edit and Find/replace stay disabled with a selection tooltip when the selection is empty', () => {
	const kinds = [];
	const items = mod.trackEditMenuItems(0, (kind) => kinds.push(kind));
	const byId = Object.fromEntries(items.map((item) => [item.id, item]));
	assert.equal(byId['bulk-edit'].run, undefined);
	assert.equal(byId['find-replace'].run, undefined);
	assert.equal(byId['bulk-edit'].title, EMPTY_TITLE);
	assert.equal(byId['find-replace'].title, EMPTY_TITLE);
	assert.equal(typeof byId['mytag'].run, 'function');
	byId['mytag'].run();
	assert.deepEqual(kinds, ['mytag']);
});

test('TrackContextMenu spreads the helper and BrowserPanel passes the toolbar opener', () => {
	const menu = readFileSync(`${SRC}/lib/components/rb/browser/TrackContextMenu.svelte`, 'utf8');
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	const contextMenu = readFileSync(`${SRC}/lib/components/rb/ContextMenu.svelte`, 'utf8');
	assert.match(menu, /trackEditMenuItems\(targetIds\.length, onopeneditmodal\)/);
	assert.match(panel, /onopeneditmodal=\{\(kind\) => void openEditModal\(kind\)\}/);
	assert.match(contextMenu, /item\.title \?\? \(unavailable \? 'not implemented - see PARITY-TODO' : item\.label\)/);
});
