/**
 * LIBUX-8626 (the maintainer, Wed 4 Nov 2026): the undo popover lists the FULL history
 * newest first, opens on a 1 s hover and stays open over button or popover, a
 * row click undoes back to it, a row hover shows what it touched, and a
 * playlist row removal splices in place with the selection on its neighbour.
 *
 * [if] the newest edit is not the first row of the list [then ⛔]
 * [if] clicking an applied row would not undo it and every newer edit, as the
 *   row title says [then ⛔]
 * [if] POST /playlist-history/jump is not the client's wire call [then ⛔]
 * [if] the agent bus cannot jump the history or reveal a track [then ⛔]
 * [if] removing a row does not select the row below it (or above, at the end),
 *   or the remaining rows are not renumbered [then ⛔]
 * [if] the removal reloads the pane or never unmarks the red rows [then ⛔]
 * [if] a refresh during the animation drops a surviving neighbour occupying a
 *   pre-write order [then ⛔]
 */
// requirement: LIBUX-8626
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://libux-hist.example.test';

let history;
let removal;
let ipc;
let originalFetch;

before(async () => {
	history = await loadTypeScriptModule('src/lib/rb/playlist-history.ts', { viteApiBase: API_BASE });
	removal = await loadTypeScriptModule('src/lib/components/rb/browser/pane-row-removal.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

const entry = (n, op = 'memberships', stable_ids = []) => ({
	command_id: `c${n}`,
	op,
	playlist_id: 'inbox',
	ts: '2026-11-04T17:00:00Z',
	label: `edit ${n}`,
	stable_ids
});

// ---------------------------------------------------------------- history list

test('LIBUX-8626: the list is newest first and each row says what a click does', () => {
	const rows = history.historyRowsNewestFirst([entry(1), entry(2), entry(3), entry(4)], 3);
	assert.deepEqual(
		rows.map((row) => [row.entry.command_id, row.applied, row.next]),
		[
			['c4', false, false],
			['c3', true, true],
			['c2', true, false],
			['c1', true, false]
		]
	);
	assert.equal(rows[1].action, 'Click to undo this edit');
	assert.equal(rows[3].action, 'Click to undo this edit and 2 newer');
	assert.equal(rows[0].action, 'Click to redo this edit');
});

test('LIBUX-8626: hover targets the playlist and its first touched track, never a missing playlist', () => {
	const [removeRow] = history.historyRowsNewestFirst([entry(1, 'memberships', ['t-9', 't-8'])], 1);
	assert.deepEqual(history.historyEntryNavTarget(removeRow), { playlist_id: 'inbox', stable_id: 't-9' });
	const [rename] = history.historyRowsNewestFirst([entry(1, 'rename')], 1);
	assert.deepEqual(history.historyEntryNavTarget(rename), { playlist_id: 'inbox' });
	const [undoneCreate] = history.historyRowsNewestFirst([entry(1, 'create')], 0);
	assert.equal(history.historyEntryNavTarget(undoneCreate), null);
	const [liveDelete] = history.historyRowsNewestFirst([entry(1, 'delete')], 1);
	assert.equal(history.historyEntryNavTarget(liveDelete), null);
});

test('LIBUX-8626: jumpPlaylistHistory posts /playlist-history/jump with the command id', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.json();
		return new Response(JSON.stringify({ cursor: 1, can_undo: true, can_redo: true, steps: [] }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	const result = await history.jumpPlaylistHistory('c2');
	assert.equal(seen.url, `${API_BASE}/api/v1/playlist-history/jump`);
	assert.equal(seen.method, 'POST');
	assert.deepEqual(body, { command_id: 'c2' });
	assert.equal(result.cursor, 1);
});

test('LIBUX-8626: the popover opens on 1 s hover intent and is interactive (no pointer-events: none)', () => {
	assert.equal(history.HISTORY_HOVER_OPEN_MS, 1000);
	const tabs = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/LibrarySourceTabs.svelte', import.meta.url)),
		'utf8'
	);
	const button = tabs.slice(tabs.indexOf('data-testid="playlist-undo"'), tabs.indexOf('data-testid="playlist-redo"'));
	assert.match(button, /onmouseenter=\{intendHistory\}/);
	assert.match(button, /onmouseleave=\{releaseHistory\}/);
	const popover = tabs.slice(tabs.indexOf('class="history-popover"'), tabs.indexOf('</ol>'));
	assert.match(popover, /onmouseenter=\{keepHistoryOpen\}/);
	assert.match(popover, /onmouseleave=\{releaseHistory\}/);
	assert.match(popover, /\{#each historyRows as row/);
	assert.doesNotMatch(tabs, /\.history-popover \{[^}]*pointer-events: none;/);
});

// ---------------------------------------------------------------- agent bus

test('LIBUX-8626: playlist_history_jump and browser_select_playlist stable_id parse strictly', () => {
	assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'playlist_history_jump', command_id: 'c2' }), {
		type: 'playlist_history_jump',
		command_id: 'c2'
	});
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'playlist_history_jump' }), /command_id/);
	assert.deepEqual(
		ipc.parsePerformanceCommandForTest({ type: 'browser_select_playlist', playlist_id: 'p', stable_id: 't' }),
		{ type: 'browser_select_playlist', playlist_id: 'p', stable_id: 't' }
	);
	assert.throws(
		() => ipc.parsePerformanceCommandForTest({ type: 'browser_select_playlist', playlist_id: 'p', stable_id: '' }),
		/stable_id/
	);
});

test('LIBUX-8626: the bus forwards a jump to the history adapter and a reveal to the browser', async () => {
	const jumps = [];
	const selects = [];
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	const unregisterHistory = ipc.registerPlaylistHistoryAdapter({
		undo: async () => {},
		redo: async () => {},
		jump: async (commandId) => {
			jumps.push(commandId);
		}
	});
	const unregisterBrowser = ipc.registerPerformanceBrowserAdapter({
		selectPlaylist: async (playlistId, stableId) => {
			selects.push([playlistId, stableId]);
		},
		readSnapshot: () => ({ search: null, sort: null, selected_row: null, load: null })
	});
	try {
		await ipc.dispatchPerformanceCommand({ type: 'playlist_history_jump', command_id: 'c7' });
		await ipc.dispatchPerformanceCommand({ type: 'browser_select_playlist', playlist_id: 'inbox', stable_id: 't-1' });
		assert.deepEqual(jumps, ['c7']);
		assert.deepEqual(selects, [['inbox', 't-1']]);
	} finally {
		unregisterBrowser();
		unregisterHistory();
		uninstall();
		delete globalThis.window;
	}
});

// ---------------------------------------------------------------- row removal

const slots = (...orders) => orders.map((order) => ({ stable_id: `t-${order}`, order }));

test('LIBUX-8626: selection moves to the row below, or above when the bottom row went', () => {
	assert.deepEqual(removal.selectionAfterRemoval(slots(1, 2, 3, 4), new Set([2])), { stable_id: 't-3', order: 2 });
	assert.deepEqual(removal.selectionAfterRemoval(slots(1, 2, 3, 4), new Set([4])), { stable_id: 't-3', order: 3 });
	assert.deepEqual(removal.selectionAfterRemoval(slots(1, 2, 3, 4), new Set([2, 3])), { stable_id: 't-4', order: 2 });
	assert.equal(removal.selectionAfterRemoval(slots(1), new Set([1])), null);
	// On-screen order wins over membership order (a sorted pane).
	assert.deepEqual(removal.selectionAfterRemoval(slots(3, 1, 2), new Set([1])), { stable_id: 't-2', order: 1 });
});

test('LIBUX-8626: removed rows leave and the rest renumber 1..n, untouched rows keep identity', () => {
	const rows = slots(1, 2, 3);
	const kept = removal.withoutRemovedRows(rows, new Set([2]));
	assert.deepEqual(kept.map((r) => [r.stable_id, r.order]), [['t-1', 1], ['t-3', 2]]);
	assert.equal(kept[0], rows[0]);
});

test('LIBUX-8626: withoutRemovedItemIds drops by membership id; no-op when refresh already renumbered', () => {
	const rows = slots(1, 2, 3).map((row) => ({ ...row, item_id: `i-${row.order}` }));
	const kept = removal.withoutRemovedItemIds(rows, new Set(['i-2']));
	assert.deepEqual(kept.map((r) => [r.stable_id, r.order]), [['t-1', 1], ['t-3', 2]]);
	assert.equal(kept[0], rows[0]);
	const refreshed = [
		{ stable_id: 't-1', order: 1, item_id: 'i-1' },
		{ stable_id: 't-3', order: 2, item_id: 'i-3' }
	];
	const unchanged = removal.withoutRemovedItemIds(refreshed, new Set(['i-2']));
	assert.equal(unchanged[0], refreshed[0]);
	assert.equal(unchanged[1], refreshed[1]);
	assert.deepEqual(unchanged.map((r) => r.order), [1, 2]);
});

function fakePane(rows) {
	return {
		playlist_id: 'inbox',
		rows,
		etag: '"old"',
		selected_id: rows[1].stable_id,
		selected_ids: [rows[1].stable_id],
		selected_order: rows[1].order,
		selected_orders: [rows[1].order],
		select(stable_id, _extend, _range, _ids, order) {
			this.selected_id = stable_id;
			this.selected_ids = [stable_id];
			this.selected_order = order;
			this.selected_orders = [order];
		}
	};
}

test('LIBUX-8626: removeRowsInPlace marks, writes once, splices, and selects the neighbour', async () => {
	const rows = slots(1, 2, 3).map((row) => ({ ...row, item_id: `i-${row.order}` }));
	const pane = fakePane(rows);
	const marks = [];
	const writes = [];
	const outcome = await removal.removeRowsInPlace({
		pane,
		rows: [rows[1]],
		visible: rows,
		remove: async (playlistId, itemIds) => {
			writes.push([playlistId, itemIds]);
			return { etag: '"new"' };
		},
		markRemoving: (keys) => marks.push([...keys]),
		rowKey: (row) => `${row.stable_id}:${row.order}`,
		wait: async () => {}
	});
	assert.deepEqual(outcome.rows.map((r) => [r.stable_id, r.order]), [['t-2', 2]]);
	assert.deepEqual([outcome.playlist_id, outcome.etag_after], ['inbox', '"new"']);
	assert.deepEqual(writes, [['inbox', ['i-2']]]);
	assert.deepEqual(marks, [['t-2:2'], []]);
	assert.deepEqual(pane.rows.map((r) => r.stable_id), ['t-1', 't-3']);
	assert.equal(pane.etag, '"new"');
	assert.deepEqual([pane.selected_id, pane.selected_order], ['t-3', 2]);
});

test('LIBUX-8626: removeRowsInPlace keeps survivors when a library refresh interleaves the animation', async () => {
	const rows = slots(1, 2, 3).map((row) => ({ ...row, item_id: `i-${row.order}` }));
	const pane = fakePane(rows);
	let finish;
	const outcomePromise = removal.removeRowsInPlace({
		pane,
		rows: [rows[1]],
		visible: rows,
		remove: async () => ({ etag: '"new"' }),
		markRemoving: () => {},
		rowKey: (row) => `${row.stable_id}:${row.order}`,
		wait: () => new Promise((resolve) => (finish = resolve))
	});
	await new Promise((resolve) => setImmediate(resolve));
	pane.rows = [
		{ stable_id: 't-1', order: 1, item_id: 'i-1' },
		{ stable_id: 't-3', order: 2, item_id: 'i-3' }
	];
	finish();
	const outcome = await outcomePromise;
	assert.deepEqual(
		pane.rows.map((r) => [r.stable_id, r.order]),
		[
			['t-1', 1],
			['t-3', 2]
		]
	);
	assert.deepEqual(outcome.rows.map((r) => [r.stable_id, r.order, r.item_id]), [['t-2', 2, 'i-2']]);
	assert.deepEqual([pane.selected_id, pane.selected_order], ['t-3', 2]);
	assert.equal(pane.etag, '"new"');
});

test('LIBUX-8626 control: a failed write unmarks the rows and leaves the pane untouched', async () => {
	const rows = slots(1, 2, 3).map((row) => ({ ...row, item_id: `i-${row.order}` }));
	const pane = fakePane(rows);
	const marks = [];
	await assert.rejects(
		removal.removeRowsInPlace({
			pane,
			rows: [rows[1]],
			visible: rows,
			remove: async () => {
				throw new Error('boom');
			},
			markRemoving: (keys) => marks.push([...keys]),
			rowKey: (row) => `${row.stable_id}:${row.order}`,
			wait: async () => {}
		}),
		/boom/
	);
	assert.deepEqual(marks.at(-1), []);
	assert.equal(pane.rows, rows);
	assert.equal(pane.etag, '"old"');
});

test('LIBUX-8626: BrowserPanel removes rows in place, never through a pane reload', () => {
	const panel = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)),
		'utf8'
	);
	const body = panel.slice(
		panel.indexOf('async function _removeMemberships'),
		panel.indexOf('function _revealTrack')
	);
	assert.match(body, /removeRowsInPlace\(/);
	assert.doesNotMatch(body, /_loadPane\(/, 'a reload resets rows, selection and scroll');
	assert.match(panel, /onremoverows=\{_removeMemberships\}/);
});
