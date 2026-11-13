/**
 * Issue #10028: the Tag List prep list in the browser (rekordbox Tag List, mimicked).
 *
 * The fetch client runs against a recorded-shape fake fetch (no server here); the
 * rail renders through Svelte SSR, so clicks, the pane load and the header buttons
 * are not exercised (e2e not run).
 *
 * [if] the row menu has no multi-row Add to Tag List naming the tag_list_add bus command [then] broken.
 * [if] the bus refuses a valid add or accepts an empty one [then] agents and the menu disagree - broken.
 * [if] the client does not hit the #14510 routes with their bodies [then] the UI talks to nothing - broken.
 * [if] the rail entry does not show the Tag List with its count [then] the source is invisible - broken.
 * [if] the Tag List rows are not the listed tracks in order [then] the table is not the Tag List - broken.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://tag-list.example.test';

const LISTED = {
	count: 2,
	cap: 100,
	items: [
		{ position: 1, stable_id: 'b', title: 'Second' },
		{ position: 2, stable_id: 'a', title: 'First' }
	]
};

let api;
let paneHelpers;
let registry;
let commands;
let guards;
let ssr;

before(async () => {
	api = await loadTypeScriptModule('src/lib/tag-list/tag-list-api.ts', { viteApiBase: API_BASE });
	paneHelpers = await loadTypeScriptModule('src/lib/tag-list/tag-list-pane.ts');
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
	commands = await loadTypeScriptModule('src/lib/context-menu/commands.ts');
	guards = await loadTypeScriptModule('src/lib/rb/performance-ipc-guards.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as IconRail } from '$lib/components/rb/browser/IconRail.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

function fakeFetch(calls, status, body) {
	return async (url, init = {}) => {
		calls.push({ url: String(url), method: init.method ?? 'GET', body: init.body });
		return new Response(JSON.stringify(body), {
			status,
			headers: { 'Content-Type': 'application/json' }
		});
	};
}

const TRACK_CTX = {
	selection: 'many',
	source: 'collection',
	audio: 'present',
	stems: 'none',
	tracklistHidden: 'shown',
	workspace: 'performance'
};

test('the track row menu offers Add to Tag List for a multi-row selection on the tag_list_add bus command', () => {
	// [if] three selected rows get no enabled Add to Tag List on tag_list_add [then] the row menu cannot feed the list, [else stop].
	const item = registry
		.menuFor({ kind: 'track', count: 3, rows: 3 }, TRACK_CTX)
		.find((entry) => entry.id === 'add-tag-list');
	assert.ok(item, 'Add to Tag List is in the track menu');
	assert.equal(item.label, 'Add to Tag List');
	assert.equal(item.enabled, true);
	assert.deepEqual(commands.MENU_COMMANDS[item.command], { via: 'ipc', type: 'tag_list_add' });
	const usb = registry
		.menuFor(
			{ kind: 'track', count: 1, rows: 1 },
			{ ...TRACK_CTX, selection: 'one', source: 'usb' }
		)
		.find((entry) => entry.id === 'add-tag-list');
	assert.equal(usb, undefined, 'a read-only USB row does not offer it');
});

test('the tag_list_add bus command takes stable_ids and refuses an empty or malformed list', () => {
	// [if] the guard drops ids or accepts [] or a blank id [then] agents and the menu post different adds, [else stop].
	assert.deepEqual(guards._parseCommand({ type: 'tag_list_add', stable_ids: ['b', 'a'] }), {
		type: 'tag_list_add',
		stable_ids: ['b', 'a']
	});
	for (const bad of [[], [''], 'a', [1]]) {
		assert.throws(() => guards._parseCommand({ type: 'tag_list_add', stable_ids: bad }), TypeError);
	}
	assert.throws(() => guards._parseCommand({ type: 'tag_list_add', stable_ids: ['a'], extra: 1 }));
});

test('the client reads, adds, removes, clears and saves through the #14510 routes', async () => {
	// [if] a call misses its route, method or body [then] the UI and opendj taglist act on different lists, [else stop].
	const calls = [];
	assert.deepEqual(await api.fetchTagList(fakeFetch(calls, 200, LISTED)), LISTED);
	await api.addToTagList(['a', 'b', 'a'], fakeFetch(calls, 200, { ...LISTED, added: ['a'] }));
	await api.removeFromTagList('fixture:1', fakeFetch(calls, 200, LISTED));
	await api.clearTagList(fakeFetch(calls, 200, { removed: 2 }));
	const saved = await api.saveTagListAsPlaylist(
		'Friday prep',
		fakeFetch(calls, 201, { playlist_id: 'p1', name: 'Friday prep', track_count: 2 })
	);
	assert.equal(saved.track_count, 2);
	assert.deepEqual(
		calls.map((c) => [c.method, c.url.replace(API_BASE, ''), c.body ?? null]),
		[
			['GET', '/api/v1/taglist', null],
			['POST', '/api/v1/taglist/items', JSON.stringify({ stable_ids: ['a', 'b'] })],
			['DELETE', '/api/v1/taglist/items/fixture%3A1', null],
			['DELETE', '/api/v1/taglist', null],
			['POST', '/api/v1/taglist:save', JSON.stringify({ name: 'Friday prep' })]
		]
	);
});

test('an add past the cap surfaces the 409 TAGLIST_FULL code and an empty add never posts', async () => {
	// [if] the 101st track is swallowed or [] is posted [then] the DJ thinks it was added, [else stop].
	const calls = [];
	const full = {
		detail: { code: 'TAGLIST_FULL', cap: 100, message: 'the Tag List holds at most 100 tracks' }
	};
	await assert.rejects(api.addToTagList(['z'], fakeFetch(calls, 409, full)), (error) => {
		assert.equal(error.status, 409);
		assert.equal(error.code, 'TAGLIST_FULL');
		return true;
	});
	await assert.rejects(api.addToTagList([], fakeFetch(calls, 200, LISTED)), /at least one/);
	assert.equal(calls.length, 1);
});

test('the rail shows a lit Tag List entry with its count, and no badge while empty', () => {
	// [if] the rail has no Tag List entry or its count is missing [then] the source cannot be found, [else stop].
	const render = (props) =>
		ssr.render(ssr.IconRail, {
			props: { source: 'collection', onspotify: () => {}, oncollection: () => {}, ...props }
		}).body;
	const html = render({ tagListCount: 12, tagListOpen: true });
	const button = /<button\b[^>]*data-testid="rail-tag-list"[^>]*>/.exec(html)?.[0] ?? '';
	assert.match(button, /title="Tag List \(12 of 100\) - /);
	assert.match(button, /aria-pressed="true"/);
	assert.match(html, /data-testid="rail-tag-list-count"[^>]*>12</);
	assert.doesNotMatch(render({ tagListCount: 0 }), /rail-tag-list-count/);
	assert.match(render({ tagListCount: null }), /data-testid="rail-tag-list"/);
});

test('the Tag List pane lists its tracks in Tag List order and drops ones gone from the library', () => {
	// [if] rows are reordered, renumbered wrong or keep a deleted track [then] the table is not the Tag List, [else stop].
	const index = [
		{ stable_id: 'a', title: 'First' },
		{ stable_id: 'b', title: 'Second' }
	];
	const listed = [...LISTED.items, { position: 3, stable_id: 'gone', title: null }];
	const rows = paneHelpers.tagListRows(listed, index, (item, order) => ({
		id: item.stable_id,
		order
	}));
	assert.deepEqual(rows, [
		{ id: 'b', order: 1 },
		{ id: 'a', order: 2 }
	]);
	const node = paneHelpers.tagListNode(2);
	assert.equal(node.playlist_id, paneHelpers.TAG_LIST_ID);
	assert.equal(node.kind, 'tag_list');
	assert.equal(paneHelpers.isTagListId('taglist:Warmup'), false, 'MyTag ids are not the Tag List');
	assert.deepEqual(
		guards._parseCommand({ type: 'browser_select_playlist', playlist_id: 'tag-list' }),
		{
			type: 'browser_select_playlist',
			playlist_id: 'tag-list'
		}
	);
	assert.equal(
		paneHelpers.tagListAddedMessage(3, 2, 7),
		'Tag List: 2 added, 1 already listed (7 listed)'
	);
});
