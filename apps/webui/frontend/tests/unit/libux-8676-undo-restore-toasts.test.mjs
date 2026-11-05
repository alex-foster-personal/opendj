/**
 * LIBUX-8676 (the maintainer, Wed 4 Nov 2026): a plain click collapses a multi-selection
 * even when WebKit turned it into a zero-distance drag, the 1/2/3/4 load box
 * stays hidden while several rows are selected, the row delete animates over
 * 200 ms, undoing a removal puts the rows back in place at once with a green
 * expand and selects them, and track adds, removes, undo, redo and history
 * jumps each raise ONE toast.
 *
 * [if] a press that barely moved and dropped nowhere does not collapse a
 *   multi-selection, or a real drag / modified press does [then ⛔]
 * [if] the load box can show with more than one row selected [then ⛔]
 * [if] the remove or restore animation is not 200 ms [then ⛔]
 * [if] an undone removal does not come back at its old slots, selected, before
 *   the undo write returns [then ⛔]
 * [if] a stale history read or failed undo leaves restored rows on screen [then ⛔]
 * [if] undo / redo / jump / add / remove raise no toast, or a burst is not one
 *   grouped toast [then ⛔]
 */
// requirement: LIBUX-8676
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let removal;
let apply;
let click;

before(async () => {
	removal = await loadTypeScriptModule('src/lib/components/rb/browser/pane-row-removal.ts');
	apply = await loadTypeScriptModule('src/lib/rb/playlist-history-apply.ts');
	click = await loadTypeScriptModule('src/lib/components/rb/browser/row-click-intent.ts');
});

const read = (rel) => readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');
const slots = (...orders) => orders.map((order) => ({ stable_id: `t-${order}`, order }));

function fakePane(rows, etag = '"after"') {
	return {
		playlist_id: 'inbox',
		rows,
		etag,
		selected_id: null,
		selected_ids: [],
		selected_order: null,
		selected_orders: [],
		select(stable_id, extend, _range, _ids, order) {
			this.selected_id = stable_id;
			this.selected_order = order;
			this.selected_ids = extend ? [...this.selected_ids, stable_id] : [stable_id];
			this.selected_orders = extend ? [...this.selected_orders, order] : [order];
		}
	};
}

// ---------------------------------------------------------------- click intent

const press = (over = {}) => ({
	button: 0,
	clientX: 100,
	clientY: 50,
	shiftKey: false,
	metaKey: false,
	ctrlKey: false,
	altKey: false,
	...over
});

test('LIBUX-8676: a micro-drag that dropped nowhere collapses a multi-selection', () => {
	const mark = click.markRowPress(press(), 'k');
	const base = { mark, key: 'k', dropEffect: 'none', selectedCount: 3 };
	assert.equal(click.dragEndIsCollapsingClick({ ...base, end: { clientX: 103, clientY: 52 } }), true);
	assert.equal(click.dragEndIsCollapsingClick({ ...base, end: { clientX: 100, clientY: 50 } }), true);
});

test('LIBUX-8676 control: real drags, single rows and modified presses never collapse', () => {
	const mark = click.markRowPress(press(), 'k');
	const near = { clientX: 102, clientY: 50 };
	assert.equal(click.dragEndIsCollapsingClick({ mark, key: 'k', end: { clientX: 130, clientY: 50 }, dropEffect: 'none', selectedCount: 3 }), false);
	assert.equal(click.dragEndIsCollapsingClick({ mark, key: 'k', end: near, dropEffect: 'copy', selectedCount: 3 }), false);
	assert.equal(click.dragEndIsCollapsingClick({ mark, key: 'k', end: near, dropEffect: 'none', selectedCount: 1 }), false);
	assert.equal(click.dragEndIsCollapsingClick({ mark, key: 'other', end: near, dropEffect: 'none', selectedCount: 3 }), false);
	assert.equal(click.markRowPress(press({ shiftKey: true }), 'k'), null);
	assert.equal(click.markRowPress(press({ metaKey: true }), 'k'), null);
	assert.equal(click.markRowPress(press({ button: 2 }), 'k'), null);
	assert.equal(click.dragEndIsCollapsingClick({ mark: null, key: 'k', end: near, dropEffect: 'none', selectedCount: 3 }), false);
});

test('LIBUX-8676: TrackTable wires the press mark and the dragend collapse', () => {
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /onpointerdown=\{\(e\) => \(_rowPress = markRowPress\(e, trackRowKey\(row\)\)\)\}/);
	assert.match(table, /ondragend=\{\(e\) => onRowDragEnd\(e, row\)\}/);
	const body = table.slice(table.indexOf('function onRowDragEnd'), table.indexOf('function onGripDragStart'));
	assert.match(body, /dragEndIsCollapsingClick\(/);
	assert.match(body, /onselectrow\(row\)/);
});

// ---------------------------------------------------------------- load box

test('LIBUX-8676: the 1/2/3/4 load box is hidden while more than one row is selected', () => {
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /class:rb-multi-selected=\{selectedOrderSet\.size > 1\}/);
	assert.match(table, /table\.rb-multi-selected \.deck-btns \{\s*visibility: hidden;/);
});

// ---------------------------------------------------------------- animations

test('LIBUX-8676: remove and restore both animate over 200 ms, CSS matching the constants', () => {
	assert.equal(removal.ROW_REMOVE_ANIMATION_MS, 200);
	assert.equal(removal.ROW_RESTORE_ANIMATION_MS, 200);
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /animation: rb-row-remove 200ms/);
	assert.match(table, /animation: rb-row-restore 200ms/);
	assert.match(table, /@keyframes rb-row-restore \{[^@]*var\(--rb-green\)/);
	assert.match(table, /class:rb-row-restoring=\{restoringRowKeys\.has\(trackRowKey\(row\)\)\}/);
});

// ---------------------------------------------------------------- restore

test('LIBUX-8676: withRestoredRows is the exact inverse of withoutRemovedRows', () => {
	const rows = slots(1, 2, 3, 4, 5);
	for (const gone of [[1], [3], [5], [2, 4], [1, 5], [1, 2, 3, 4, 5]]) {
		const removed = new Set(gone);
		const kept = removal.withoutRemovedRows(rows, removed);
		const back = removal.withRestoredRows(kept, rows.filter((r) => removed.has(r.order)));
		assert.deepEqual(back.map((r) => [r.stable_id, r.order]), rows.map((r) => [r.stable_id, r.order]), `gone ${gone}`);
	}
	assert.throws(() => removal.withRestoredRows(slots(1), [{ stable_id: 'x', order: 5 }]), /no row for slot/);
});

const entry = (over = {}) => ({
	command_id: 'c-9',
	op: 'memberships',
	playlist_id: 'inbox',
	ts: 'now',
	label: "Remove 'T2' from 'INBOX'",
	stable_ids: ['t-2', 't-4'],
	...over
});

test('LIBUX-8676: only the pane\'s last removal, unchanged since, is restorable', () => {
	const last = { playlist_id: 'inbox', rows: [slots(2)[0], slots(4)[0]], etag_after: '"after"' };
	const older = { playlist_id: 'inbox', rows: slots(1), etag_after: '"older"' };
	const pane = { playlist_id: 'inbox', etag: '"after"' };
	assert.equal(removal.restorableRemoval([older, last], entry(), pane), last);
	assert.equal(removal.restorableRemoval([], entry(), pane), null);
	assert.equal(removal.restorableRemoval([last], entry({ op: 'rename' }), pane), null);
	assert.equal(removal.restorableRemoval([last], entry({ playlist_id: 'other' }), pane), null);
	assert.equal(removal.restorableRemoval([last], entry({ stable_ids: ['t-2'] }), pane), null);
	assert.equal(removal.restorableRemoval([last], entry({ stable_ids: undefined }), pane), null);
	assert.equal(removal.restorableRemoval([last], entry(), { ...pane, etag: '"newer edit"' }), null);
	assert.equal(removal.restorableRemoval([last], entry(), { ...pane, playlist_id: 'other' }), null);
});

test('LIBUX-8676: restoreRowsInPlace puts rows back green and selected, commit takes the etag', async () => {
	const all = slots(1, 2, 3, 4);
	const gone = [all[1], all[3]];
	const pane = fakePane(removal.withoutRemovedRows(all, new Set([2, 4])));
	const marks = [];
	let finish;
	const pending = removal.restoreRowsInPlace({
		pane,
		removal: { playlist_id: 'inbox', rows: gone, etag_after: '"after"' },
		markRestoring: (keys) => marks.push([...keys]),
		rowKey: (row) => `${row.stable_id}:${row.order}`,
		wait: () => new Promise((resolve) => (finish = resolve))
	});
	assert.deepEqual(pane.rows.map((r) => r.stable_id), ['t-1', 't-2', 't-3', 't-4']);
	assert.deepEqual(marks, [['t-2:2', 't-4:4']]);
	assert.deepEqual(pane.selected_ids, ['t-2', 't-4']);
	assert.deepEqual(pane.selected_orders, [2, 4]);
	pending.commit('"restored"');
	assert.equal(pane.etag, '"restored"');
	finish();
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(marks.at(-1), []);
});

test('LIBUX-8676 control: revert takes the restored rows back out and the old selection back', () => {
	const kept = slots(1, 2);
	const pane = fakePane(kept);
	pane.select('t-1', false, false, [], 1);
	const pending = removal.restoreRowsInPlace({
		pane,
		removal: { playlist_id: 'inbox', rows: [{ stable_id: 'x', order: 2 }], etag_after: '"after"' },
		markRestoring: () => {},
		rowKey: (row) => `${row.stable_id}:${row.order}`,
		wait: async () => {}
	});
	assert.equal(pane.rows.length, 3);
	pending.revert();
	assert.equal(pane.rows, kept);
	assert.deepEqual([pane.selected_id, pane.selected_ids], ['t-1', ['t-1']]);
	assert.equal(pane.etag, '"after"');
});

// ---------------------------------------------------------------- history step + toasts

function harness(result) {
	const log = [];
	const toasts = [];
	return {
		log,
		toasts,
		send: async () => {
			log.push('send');
			if (result instanceof Error) throw result;
			return result;
		},
		restorer: (seen) => {
			log.push(`restore ${seen.command_id}`);
			return { commit: (etag) => log.push(`commit ${etag}`), revert: () => log.push('revert') };
		},
		toast: (message, action) => toasts.push([message, action?.label])
	};
}

test('LIBUX-8676: undo restores BEFORE the write, commits on the same entry, and toasts with Redo', async () => {
	const h = harness({ command_id: 'c-9', op: 'memberships', etag: '"e"' });
	await apply.applyHistoryStep({ action: 'undo', expected: entry(), send: h.send, restorer: h.restorer, toast: h.toast, opposite: () => {} });
	assert.deepEqual(h.log, ['restore c-9', 'send', 'commit "e"']);
	assert.deepEqual(h.toasts, [["Undone: Remove 'T2' from 'INBOX'", 'Redo']]);
});

test('LIBUX-8676 control: a stale read reverts, a failed undo reverts and rethrows without a toast', async () => {
	const stale = harness({ command_id: 'c-8', op: 'rename' });
	await apply.applyHistoryStep({ action: 'undo', expected: entry(), send: stale.send, restorer: stale.restorer, toast: stale.toast, opposite: () => {} });
	assert.deepEqual(stale.log, ['restore c-9', 'send', 'revert']);
	assert.deepEqual(stale.toasts, [['Undone: rename', 'Redo']]);

	const failed = harness(new Error('boom'));
	await assert.rejects(
		apply.applyHistoryStep({ action: 'undo', expected: entry(), send: failed.send, restorer: failed.restorer, toast: failed.toast, opposite: () => {} }),
		/boom/
	);
	assert.deepEqual(failed.log, ['restore c-9', 'send', 'revert']);
	assert.deepEqual(failed.toasts, []);
});

test('LIBUX-8676: redo never restores, toasts with Undo; jump and CRUD messages read plainly', async () => {
	const h = harness({ command_id: 'c-9', op: 'memberships' });
	await apply.applyHistoryStep({ action: 'redo', expected: entry(), send: h.send, restorer: h.restorer, toast: h.toast, opposite: () => {} });
	assert.deepEqual(h.log, ['send']);
	assert.deepEqual(h.toasts, [["Redone: Remove 'T2' from 'INBOX'", 'Undo']]);

	assert.equal(apply.historyJumpToastMessage({ steps: [{ action: 'undo' }, { action: 'undo' }, { action: 'undo' }] }), 'Undid 3 edits');
	assert.equal(apply.historyJumpToastMessage({ steps: [{ action: 'redo' }] }), 'Redid 1 edit');
	assert.equal(apply.historyJumpToastMessage({ steps: [] }), null);
	assert.equal(apply.removedTracksToastMessage(['Track 2'], 'INBOX'), "Removed 'Track 2' from 'INBOX'");
	assert.equal(apply.removedTracksToastMessage(['a', 'b', 'c'], 'INBOX'), "Removed 3 tracks from 'INBOX'");
	assert.equal(apply.removedTracksToastMessage(['a'], null), "Removed 'a' from playlist");
});

test('LIBUX-8676: every undo path toasts through the adapter, grouped; CRUD toasts carry Undo', () => {
	const chrome = read('src/lib/rb/playlist-history-chrome.svelte.ts');
	const adapter = chrome.slice(chrome.indexOf('registerPlaylistHistoryAdapter({'), chrome.indexOf('subscribeKind('));
	assert.equal(adapter.match(/applyHistoryStep\(/g)?.length, 2, 'undo and redo both go through applyHistoryStep');
	assert.match(adapter, /historyJumpToastMessage\(/);
	assert.match(chrome, /HISTORY_TOAST_GROUP, action\)/);
	const panel = read('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(panel, /registerPlaylistUndoRestorer\(_restoreUndoneRemoval\)/);
	assert.match(panel, /TRACK_CRUD_TOAST_GROUP, \{\s*label: 'Undo',\s*handler: undoPlaylistFromChrome/);
	assert.match(panel, /_trackCrudToast\(addToPlaylistToastMessage\(ids\.length, node\.name\)\)/);
	assert.match(panel, /_trackCrudToast\(\s*removedTracksToastMessage\(/);
	assert.match(panel, /\{restoringRowKeys\}/);
});
