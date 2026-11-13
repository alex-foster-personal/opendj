// Spec LIBM-87 (library-management-requirements.md section 08), issue #12320:
// pinned playlists draw as shortcuts above the tree, in pin order, and
// pinning never changes the tree's own rows or their order.
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let pins;

before(async () => {
	pins = await loadTypeScriptModule('src/lib/rb/playlist-tree-pins.svelte.ts', {
		viteApiBase: 'https://playlist-tree-pins.example.test'
	});
});

after(() => {
	delete globalThis.window;
});

const node = (playlist_id, name, children = []) => ({
	playlist_id,
	name,
	kind: children.length ? 'folder' : 'playlist',
	children
});

function tree() {
	return [
		node('p1', 'Warm Up'),
		node('f1', 'Gigs', [node('c1', 'Fabric Loft'), node('c2', 'Closing')]),
		node('p2', 'Peak Time')
	];
}

test('[if] playlists are pinned [then] the shortcuts follow pin order, not tree order, [else stop].', () => {
	const shown = pins.pinnedShortcutNodes(tree(), ['p2', 'p1']);
	assert.deepEqual(
		shown.map((n) => n.playlist_id),
		['p2', 'p1']
	);
});

test('[if] a playlist inside a folder is pinned [then] its shortcut is drawn too, [else stop].', () => {
	const shown = pins.pinnedShortcutNodes(tree(), ['c2']);
	assert.deepEqual(
		shown.map((n) => n.name),
		['Closing']
	);
});

test('[if] a pin names no row in the tree [then] it is skipped, not drawn blank, [else stop].', () => {
	assert.deepEqual(pins.pinnedShortcutNodes(tree(), ['gone']), []);
});

test('[if] a pin names a folder (set over HTTP or the CLI) [then] no shortcut is drawn for it, [else stop].', () => {
	assert.deepEqual(
		pins.pinnedShortcutNodes(tree(), ['f1', 'p1']).map((n) => n.playlist_id),
		['p1']
	);
});

test('[if] shortcuts are built from the tree [then] the tree rows and order stay the same, [else stop].', () => {
	const nodes = tree();
	const before = JSON.stringify(nodes);
	pins.pinnedShortcutNodes(nodes, ['p2', 'c1']);
	assert.equal(JSON.stringify(nodes), before);
});
