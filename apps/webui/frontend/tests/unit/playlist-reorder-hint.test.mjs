// requirement: LIBM-182
// [if] an editable playlist is sorted or searched [then] drag-reorder says why it is off and offers one click back to playlist order, [else stop].
// [if] the playlist is in membership order, or the pane is not an editable playlist [then] no hint shows, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const TABLE = readFileSync('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
const PANEL = readFileSync('src/lib/components/rb/BrowserPanel.svelte', 'utf8');

let hint;
let sortIpc;
before(async () => {
	hint = await loadTypeScriptModule('src/lib/components/rb/browser/playlist-reorder-hint.ts');
	sortIpc = await loadTypeScriptModule('src/lib/components/rb/browser/browser-sort-ipc.ts');
});

test('a sorted editable playlist names the sort column', () => {
	assert.equal(
		hint.reorderBlockedReason(true, 'bpm', ''),
		'Sorted by BPM - clear the sort to drag-reorder this playlist'
	);
	assert.match(hint.reorderBlockedReason(true, 'title', 'x'), /^Sorted by Track Title/);
});

test('a searched editable playlist says the search blocks it', () => {
	assert.match(hint.reorderBlockedReason(true, null, 'house'), /Search is filtering this playlist/);
});

test('control: membership order, a blank search, or a non-editable pane shows nothing', () => {
	assert.equal(hint.reorderBlockedReason(true, null, ''), null);
	assert.equal(hint.reorderBlockedReason(true, null, '   '), null);
	assert.equal(hint.reorderBlockedReason(false, 'bpm', 'house'), null);
});

test('the sort IPC restores playlist order and reports the blocker', () => {
	const saved = globalThis.window;
	globalThis.window = {};
	try {
		const state = { sort_key: 'bpm', sort_dir: 1, search: '' };
		const reason = () => hint.reorderBlockedReason(true, state.sort_key, state.search);
		const uninstall = sortIpc.installBrowserSortIpc({
			sort: (key) => {
				state.sort_key = key;
			},
			restoreOrder: () => {
				state.sort_key = null;
				state.search = '';
			},
			query: () => ({ sort_key: state.sort_key, sort_dir: state.sort_dir, visible_ids: [], reorder_blocked: reason() })
		});
		const ipc = globalThis.window.musicDjToolsBrowserSort;
		assert.match(ipc.query().reorder_blocked, /Sorted by BPM/);
		const snap = ipc.restore_order();
		assert.equal(snap.sort_key, null);
		assert.equal(snap.reorder_blocked, null);
		uninstall();
	} finally {
		globalThis.window = saved;
	}
});

test('BrowserPanel wires the reason and the restore action into TrackTable and the IPC', () => {
	assert.match(PANEL, /reorderBlockedReason\(editablePane, pane\.sort_key, pane\.search\)/);
	assert.match(PANEL, /\{reorderBlocked\}\s+onrestoreorder=\{restorePlaylistOrder\}/);
	assert.match(PANEL, /restoreOrder: restorePlaylistOrder/);
	assert.match(PANEL, /function restorePlaylistOrder\(\)[\s\S]*?p\.clearSort\(\)[\s\S]*?p\.setSearch\(''\)/);
});

test('TrackTable says why on the order cell and on drag start, and keeps the grip for reorderable panes only', () => {
	// The board: a visible handle only when reorderable, so no grip while blocked.
	assert.match(TABLE, /\{#if reorderable\}\s*<!--[^>]*-->\s*<span\s+class="grip"/);
	assert.doesNotMatch(TABLE, /grip-blocked/);
	assert.match(TABLE, /function orderCellTitle\(order: number\): string \{[\s\S]*?reorderBlocked === null[\s\S]*?\\n\$\{reorderBlocked\}/);
	assert.match(TABLE, /<td class="c-order" title=\{orderCellTitle\(row\.order\)\}>/);
	assert.match(TABLE, /if \(reorderBlocked !== null\) _showReorderHint\(\);\s+beginTrackDrag/);
	assert.match(TABLE, /\{#if reorderHintShown && reorderBlocked !== null\}[\s\S]*?Restore playlist order/);
});

test('Restore throws before hiding the hint when no restore action is wired', () => {
	const body = TABLE.slice(TABLE.indexOf('function _restoreOrder'), TABLE.indexOf('\n\t}', TABLE.indexOf('function _restoreOrder')));
	const guard = body.indexOf('if (!onrestoreorder) throw');
	assert.ok(guard !== -1, '_restoreOrder fails fast when onrestoreorder is missing');
	assert.ok(guard < body.indexOf('reorderHintShown = false'), 'the guard runs before the hint is dismissed');
	// Control: with the action wired it is called unconditionally, not optionally.
	assert.match(body, /\n\t\tonrestoreorder\(\);/);
	assert.doesNotMatch(body, /onrestoreorder\?\.\(\)/);
});
