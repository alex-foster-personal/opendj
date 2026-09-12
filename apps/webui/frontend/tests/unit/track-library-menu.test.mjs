import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/track-library-menu.ts');
});

test('confirm and toast strings mention files stay on disk', () => {
	assert.match(mod.removeFromLibraryConfirmMessage(1), /stays on disk/i);
	assert.match(mod.removeFromLibraryConfirmMessage(3), /stay on disk/i);
	assert.match(mod.removeFromLibraryToastMessage(1), /stays on disk/i);
	assert.match(mod.removeFromLibraryToastMessage(3), /stay on disk/i);
});

test('menu item carries run and forwards selected ids', () => {
	const seen = [];
	const item = mod.removeFromLibraryMenuItem(['a', 'b'], (ids) => seen.push(...ids));
	assert.equal(item.id, 'remove-library');
	assert.equal(typeof item.run, 'function');
	item.run();
	assert.deepEqual(seen, ['a', 'b']);
});

test('TrackTable and BrowserPanel wire remove-from-library helpers', () => {
	const table = readFileSync(`${SRC}/lib/components/rb/browser/TrackTable.svelte`, 'utf8');
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	assert.match(table, /removeFromLibraryMenuItem\(selected, onremovefromlibrary\)/);
	assert.match(panel, /onremovefromlibrary=\{\(ids\) => void removeFromLibraryUi\(ids\)\}/);
	assert.match(panel, /removeFromLibraryConfirmMessage/);
	assert.match(panel, /removeFromLibraryToastMessage/);
});
