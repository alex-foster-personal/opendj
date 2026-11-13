// Spec LIBM-87 (library-management-requirements.md section 08), issue #12319:
// the playlist tree's name search filters the presentation only,
// and the browser_tree_search commands give agents the same control.
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let search;
let ipc;

before(async () => {
	search = await loadTypeScriptModule('src/lib/rb/playlist-tree-search.svelte.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
		viteApiBase: 'https://playlist-tree-search.example.test'
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

test('[if] a playlist-tree query matches a playlist name [then] only that row is drawn, [else stop].', () => {
	const view = search.playlistTreeSearchView(tree(), 'peak', false);
	assert.deepEqual(
		view.nodes.map((n) => n.playlist_id),
		['p2']
	);
	assert.equal(view.active, true);
	assert.equal(view.noMatches, false);
	assert.equal(
		view.open,
		true,
		'a search opens the collapsed Playlists section to show its matches'
	);
});

test('[if] a playlist-tree query matches a child inside a folder [then] the folder shows only that child, [else stop].', () => {
	const source = tree();
	const view = search.playlistTreeSearchView(source, 'LOFT', true);
	assert.deepEqual(
		view.nodes.map((n) => n.playlist_id),
		['f1']
	);
	assert.deepEqual(
		view.nodes[0].children.map((n) => n.playlist_id),
		['c1']
	);
	assert.equal(source[1].children.length, 2, 'the source folder keeps both children');
});

test('[if] a playlist-tree query has no matches [then] no rows and the no-match state show, [else stop].', () => {
	const view = search.playlistTreeSearchView(tree(), 'zzz', true);
	assert.deepEqual(view.nodes, []);
	assert.equal(view.noMatches, true);
	assert.equal(view.open, true);
});

test('[if] the playlist-tree query is cleared [then] the same rows and the saved open state return, [else stop].', () => {
	const source = tree();
	search.playlistTreeSearchView(source, 'gigs', false);
	const cleared = search.playlistTreeSearchView(source, '   ', false);
	assert.equal(cleared.nodes, source, 'a blank query hands back the unfiltered array itself');
	assert.equal(cleared.active, false);
	assert.equal(cleared.noMatches, false);
	assert.equal(
		cleared.open,
		false,
		'the collapsed section stays collapsed once the search is gone'
	);
	assert.equal(search.playlistTreeSearchView(source, '', true).open, true);
});

test('[if] an agent sends browser_tree_search then clear [then] the readback follows both, [else stop].', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await ipc.dispatchPerformanceCommand({ type: 'browser_tree_search', query: 'loft' });
		assert.equal(ipc.queryPerformanceState().browser.tree_query, 'loft');
		await ipc.dispatchPerformanceCommand({ type: 'browser_tree_search_clear' });
		assert.equal(ipc.queryPerformanceState().browser.tree_query, '');
	} finally {
		uninstall();
	}
});

test('[if] browser_tree_search carries a blank or extra field [then] the bus refuses it, [else stop].', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'browser_tree_search', query: '  ' }),
			/non-empty/
		);
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'browser_tree_search', query: 'a', extra: 1 }),
			/unknown|unexpected|field/i
		);
		assert.throws(() => search.setPlaylistTreeSearch(3), /must be a string/);
	} finally {
		uninstall();
	}
});
