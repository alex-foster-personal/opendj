/**
 * BROWSER-12636 / issue #11319, pin 0b1e12cc01d0: Cmd/Ctrl+A selects every visible library track.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let shortcut;
let clipboard;
let sel;

before(async () => {
	shortcut = await loadTypeScriptModule('src/lib/components/rb/browser/library-edit-shortcut.ts');
	clipboard = await loadTypeScriptModule('src/lib/components/rb/browser/track-clipboard.ts');
	sel = await loadTypeScriptModule('src/lib/components/rb/browser/pane-row-selection.ts');
});

function chord(key, opts = {}) {
	const row = { tagName: 'TR', getAttribute: () => null };
	return {
		key,
		metaKey: opts.meta ?? true,
		ctrlKey: opts.ctrl ?? false,
		altKey: false,
		shiftKey: opts.shift ?? false,
		target: opts.target ?? row
	};
}

const ROWS = [
	{ stable_id: 'a', order: 1 },
	{ stable_id: 'b', order: 2 },
	{ stable_id: 'c', order: 3 }
];

test('[if] Cmd+A on a focused track row [then] select_all is the library action', () => {
	assert.equal(shortcut.libraryEditShortcut(chord('a')), 'select_all');
	assert.equal(shortcut.libraryEditShortcut(chord('a', { meta: false, ctrl: true })), 'select_all');
});

test('[if] every visible row in the pane [then] select all marks each order without shift', () => {
	const pane = {
		rows: ROWS,
		selected_id: null,
		selected_ids: [],
		selected_order: null,
		selected_orders: []
	};
	assert.equal(clipboard.selectAllRows(pane, ROWS), 3);
	assert.deepEqual(pane.selected_orders, [1, 2, 3]);
	assert.deepEqual(pane.selected_ids, ['a', 'b', 'c']);
});

test('[if] rows are selected [then] isSelectedRow drives rb-row-selected chrome', () => {
	for (const row of ROWS) {
		assert.equal(sel.isSelectedRow(row, [1, 2, 3]), true, `order ${row.order}`);
	}
	assert.equal(sel.isSelectedRow(ROWS[0], []), false);
});

test('[if] Cmd+A targets a text field [then] the library steals nothing', () => {
	const input = { tagName: 'INPUT', type: 'search', getAttribute: () => null };
	assert.equal(shortcut.libraryEditShortcut(chord('a', { target: input })), null);
});
