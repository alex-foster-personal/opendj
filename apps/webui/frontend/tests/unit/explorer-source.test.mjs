/**
 * Issue #9747: the Explorer source in the browser (rekordbox Explorer, mimicked).
 *
 * The fetch client runs against a recorded-shape fake fetch (no server here); the
 * rail and the folder tree render through Svelte SSR, so clicks and the pane load
 * are not exercised (e2e not run). Paths are fixture paths, never a real home.
 *
 * [if] the client does not hit the #14606 routes with the folder path and recursive flag [then] the UI talks to nothing - broken.
 * [if] the rail has no live Explorer entry [then] the source is invisible - broken.
 * [if] the tree does not show the folders with their counts [then] the DJ cannot browse by folder - broken.
 * [if] opening a folder does not address a pane id the bus command accepts [then] agents and the UI disagree - broken.
 * [if] the folder rows are not the listed tracks in route order [then] the table is not the folder - broken.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://explorer.example.test';
const ROOT = '/library-fixture';

const leaf = (name, path, count) => ({
	name,
	path,
	track_count: count,
	direct_track_count: count,
	children: []
});

const TREE = {
	roots: [
		{
			name: '/',
			path: '/',
			track_count: 3,
			direct_track_count: 0,
			children: [
				{
					name: 'library-fixture',
					path: ROOT,
					track_count: 3,
					direct_track_count: 1,
					children: [leaf('House', `${ROOT}/House`, 2)]
				}
			]
		}
	],
	track_count: 3,
	unlocated_count: 1
};

let api;
let pane;
let guards;
let ssr;

before(async () => {
	api = await loadTypeScriptModule('src/lib/explorer/explorer-api.ts', { viteApiBase: API_BASE });
	pane = await loadTypeScriptModule('src/lib/explorer/explorer-pane.ts');
	guards = await loadTypeScriptModule('src/lib/rb/performance-ipc-guards.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as IconRail } from '$lib/components/rb/browser/IconRail.svelte';",
			"export { default as ExplorerPanelView } from '$lib/components/rb/browser/ExplorerPanelView.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

function fakeFetch(calls, status, body) {
	return async (url, init = {}) => {
		calls.push({ url: String(url), method: init.method ?? 'GET' });
		return new Response(JSON.stringify(body), {
			status,
			headers: { 'Content-Type': 'application/json' }
		});
	};
}

test('the client reads the tree and one folder through the #14606 routes', async () => {
	// [if] a call misses its route or drops the path or recursive flag [then] the UI lists another folder, [else stop].
	const calls = [];
	assert.deepEqual(await api.fetchFolderTree(fakeFetch(calls, 200, TREE)), TREE);
	const listed = { path: `${ROOT}/House`, recursive: true, track_count: 0, items: [] };
	await api.fetchFolderTracks(`${ROOT}/House & Garage`, true, fakeFetch(calls, 200, listed));
	assert.deepEqual(
		calls.map((c) => [c.method, c.url.replace(API_BASE, '')]),
		[
			['GET', '/api/v1/library/folders'],
			[
				'GET',
				'/api/v1/library/folders/tracks?path=%2Flibrary-fixture%2FHouse+%26+Garage&recursive=true'
			]
		]
	);
});

test('a folder with no track surfaces the 404 folder_not_found and an empty path never fetches', async () => {
	// [if] the 404 is swallowed or an empty path is sent [then] the pane shows a silent empty list, [else stop].
	const calls = [];
	const missing = { detail: { error: 'folder_not_found', path: `${ROOT}/Gone` } };
	await assert.rejects(
		api.fetchFolderTracks(`${ROOT}/Gone`, false, fakeFetch(calls, 404, missing)),
		(error) => {
			assert.equal(error.status, 404);
			return true;
		}
	);
	await assert.rejects(
		api.fetchFolderTracks('', false, fakeFetch(calls, 200, {})),
		/path is required/
	);
	assert.equal(calls.length, 1);
});

test('the rail shows a live Explorer entry, lit while the folder tree is open', () => {
	// [if] the rail has no clickable Explorer entry or it never reads lit [then] the source cannot be found, [else stop].
	const render = (props) =>
		ssr.render(ssr.IconRail, {
			props: { source: 'collection', onspotify: () => {}, oncollection: () => {}, ...props }
		}).body;
	const lit =
		/<button\b[^>]*data-testid="rail-explorer"[^>]*>/.exec(render({ explorerOpen: true }))?.[0] ??
		'';
	assert.match(lit, /title="Explorer - browse the collection as folders on disk/);
	assert.match(lit, /aria-pressed="true"/);
	assert.doesNotMatch(lit, /\sdisabled/);
	const unlit = /<button\b[^>]*data-testid="rail-explorer"[^>]*>/.exec(render({}))?.[0] ?? '';
	assert.match(unlit, /aria-pressed="false"/);
});

test('the folder tree shows each visible folder with its counts, the open one marked, and the pathless count', () => {
	// [if] a folder, its count or the unlocated note is missing [then] the tree misleads the DJ, [else stop].
	const html = ssr.render(ssr.ExplorerPanelView, {
		props: {
			tree: TREE,
			loading: false,
			error: null,
			expanded: new Set(['/', ROOT]),
			activePath: `${ROOT}/House`,
			recursive: false,
			oncollection: () => {},
			ontoggle: () => {},
			onopen: () => {},
			onrecursive: () => {}
		}
	}).body;
	const paths = [...html.matchAll(/data-testid="explorer-folder" data-path="([^"]*)"/g)].map(
		(m) => m[1]
	);
	assert.deepEqual(paths, ['/', ROOT, `${ROOT}/House`]);
	assert.match(
		html,
		/class="name[^"]*">library-fixture<\/span> <span class="count[^"]*">1 \/ 3<\/span>/
	);
	assert.match(
		html,
		/class="folder[^"]*\bactive"[^>]*aria-selected="true"[^>]*>(?:(?!<\/li>).)*data-path="\/library-fixture\/House"/
	);
	assert.match(html, /data-testid="explorer-unlocated"[^>]*>\s*1 tracks have no file path/);
	const collapsed = ssr.render(ssr.ExplorerPanelView, {
		props: {
			tree: TREE,
			loading: false,
			error: null,
			expanded: new Set(),
			activePath: null,
			recursive: false
		}
	}).body;
	assert.equal(
		[...collapsed.matchAll(/data-testid="explorer-folder"/g)].length,
		1,
		'collapsed shows the root only'
	);
});

test('a folder pane id round-trips, the bus command accepts it, and the rows keep route order', () => {
	// [if] the id loses the path, the bus refuses it or rows reorder [then] agents and the table disagree, [else stop].
	const id = pane.explorerFolderId(`${ROOT}/House`);
	assert.equal(id, 'explorer:/library-fixture/House');
	assert.deepEqual(pane.explorerFolder(id), { path: `${ROOT}/House`, recursive: false });
	assert.deepEqual(pane.explorerFolder('explorer-all:C:\\Music'), {
		path: 'C:\\Music',
		recursive: true
	});
	assert.equal(pane.explorerFolder('explorer:'), null);
	assert.equal(pane.explorerFolder('taglist:House'), null);
	const node = pane.explorerFolderNode({ path: `${ROOT}/House`, recursive: true }, 2);
	assert.deepEqual(
		[node.playlist_id, node.name, node.kind],
		['explorer-all:/library-fixture/House', 'House (all)', 'explorer']
	);
	assert.equal(pane.folderLabel('C:\\'), 'C:\\');
	assert.deepEqual(guards._parseCommand({ type: 'browser_select_playlist', playlist_id: id }), {
		type: 'browser_select_playlist',
		playlist_id: id
	});
	const index = [
		{ stable_id: 'a', title: 'A' },
		{ stable_id: 'b', title: 'B' }
	];
	const listed = [{ stable_id: 'b' }, { stable_id: 'gone' }, { stable_id: 'a' }];
	assert.deepEqual(
		pane.explorerFolderRows(listed, index, (item, order) => [item.stable_id, order]),
		[
			['b', 1],
			['a', 2]
		]
	);
	assert.deepEqual(pane.ancestorPaths(TREE.roots, `${ROOT}/House`), ['/', ROOT]);
	assert.deepEqual(pane.ancestorPaths(TREE.roots, '/elsewhere'), []);
});
