import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/add-to-playlist-menu.ts');
});

test('menu item carries run and forwards selected ids in order', () => {
	const seen = [];
	const item = mod.addToPlaylistMenuItem(['a', 'b', 'c'], (ids) => seen.push(...ids));
	assert.equal(item.id, 'add-playlist');
	assert.equal(item.label, 'Add to playlist...');
	assert.equal(typeof item.run, 'function');
	item.run();
	assert.deepEqual(seen, ['a', 'b', 'c']);
});

test('run is undefined when opener is missing or selection is empty', () => {
	const withEmpty = mod.addToPlaylistMenuItem([], (ids) => ids);
	assert.equal(withEmpty.run, undefined);
	const withoutOpener = mod.addToPlaylistMenuItem(['a'], undefined);
	assert.equal(withoutOpener.run, undefined);
});

test('TrackTable and BrowserPanel wire add-to-playlist helpers', () => {
	const table = readFileSync(`${SRC}/lib/components/rb/browser/TrackTable.svelte`, 'utf8');
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	assert.match(table, /addToPlaylistMenuItem\(selected, onaddtoplaylist\)/);
	assert.match(panel, /onaddtoplaylist=/);
});
