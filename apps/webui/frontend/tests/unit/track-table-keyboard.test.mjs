// Library track list keyboard navigation (TrackTable + track-table-keyboard.ts).
// Regression lines:
// - if arrows / Home / End / PageUp / PageDown stop moving the active row, or
//   overrun either end, then the library is mouse-only again - broken
// - if Shift+arrow grows from the LAST moved row instead of a fixed anchor,
//   then a held Shift+Down selects only two rows at a time - broken
// - if keys fire while typing in the search box / an inline edit, or when the
//   key was aimed at a control inside the row, then typing moves the list - broken
// - if Enter does not take the same load path as a row double-click, keyboard
//   loads pick a different deck than the mouse - broken
// - if the table loses role=grid / aria-selected / aria-rowindex / roving
//   tabindex, screen readers stop announcing the active row - broken
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const TABLE = 'src/lib/components/rb/browser/TrackTable.svelte';
const PANEL = 'src/lib/components/rb/BrowserPanel.svelte';

let kb;
let sel;

before(async () => {
	kb = await loadTypeScriptModule('src/lib/components/rb/browser/track-table-keyboard.ts');
	sel = await loadTypeScriptModule('src/lib/components/rb/browser/pane-row-selection.ts');
});

/** A keydown aimed at the row element itself. */
function keyOnRow(key, mods = {}) {
	const row = { tagName: 'TR', getAttribute: () => null };
	return {
		key,
		shiftKey: false,
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		target: row,
		currentTarget: row,
		...mods
	};
}

// ------------------------------------------------------------ key mapping

test('arrow, Home, End, PageUp and PageDown on a row map to moves', () => {
	const cases = {
		ArrowUp: 'up',
		ArrowDown: 'down',
		Home: 'home',
		End: 'end',
		PageUp: 'pageup',
		PageDown: 'pagedown'
	};
	for (const [key, nav] of Object.entries(cases)) {
		assert.deepEqual(kb.trackTableKeyAction(keyOnRow(key)), { kind: 'move', nav, extend: false });
		assert.deepEqual(kb.trackTableKeyAction(keyOnRow(key, { shiftKey: true })), {
			kind: 'move',
			nav,
			extend: true
		});
	}
});

test('Enter on a row maps to the double-click load, carrying Shift and Cmd/Ctrl', () => {
	assert.deepEqual(kb.trackTableKeyAction(keyOnRow('Enter')), {
		kind: 'load',
		shift: false,
		replace: false
	});
	assert.deepEqual(kb.trackTableKeyAction(keyOnRow('Enter', { shiftKey: true })), {
		kind: 'load',
		shift: true,
		replace: false
	});
	assert.deepEqual(kb.trackTableKeyAction(keyOnRow('Enter', { metaKey: true })), {
		kind: 'load',
		shift: false,
		replace: true
	});
});

test('input-focus guard: keys typed in a text field are never table keys', () => {
	const row = { tagName: 'TR', getAttribute: () => null };
	const search = { tagName: 'INPUT', type: 'search', getAttribute: () => null };
	const inlineEdit = { tagName: 'DIV', isContentEditable: true, getAttribute: () => null };
	const textarea = { tagName: 'TEXTAREA', getAttribute: () => null };
	for (const target of [search, inlineEdit, textarea]) {
		for (const key of ['ArrowDown', 'ArrowUp', 'Home', 'End', 'PageDown', 'Enter']) {
			// Even when the field sits on the row's own element path.
			const e = { ...keyOnRow(key), target, currentTarget: target };
			assert.deepEqual(kb.trackTableKeyAction(e), { kind: 'none' }, `${key} in ${target.tagName}`);
			const bubbled = { ...keyOnRow(key), target, currentTarget: row };
			assert.deepEqual(kb.trackTableKeyAction(bubbled), { kind: 'none' });
		}
	}
});

test('a key bubbling from a control inside the row belongs to that control', () => {
	const row = { tagName: 'TR', getAttribute: () => null };
	const deckButton = { tagName: 'BUTTON', getAttribute: () => null };
	for (const key of ['Enter', 'ArrowDown', 'Home']) {
		const e = { ...keyOnRow(key), target: deckButton, currentTarget: row };
		assert.deepEqual(kb.trackTableKeyAction(e), { kind: 'none' }, key);
	}
});

test('Alt and Cmd/Ctrl arrow chords and unrelated keys are left alone', () => {
	assert.deepEqual(kb.trackTableKeyAction(keyOnRow('ArrowDown', { altKey: true })), { kind: 'none' });
	assert.deepEqual(kb.trackTableKeyAction(keyOnRow('ArrowDown', { metaKey: true })), { kind: 'none' });
	assert.deepEqual(kb.trackTableKeyAction(keyOnRow('ArrowDown', { ctrlKey: true })), { kind: 'none' });
	assert.deepEqual(kb.trackTableKeyAction(keyOnRow('ArrowLeft')), { kind: 'none' });
	assert.deepEqual(kb.trackTableKeyAction(keyOnRow('a')), { kind: 'none' });
	assert.deepEqual(kb.trackTableKeyAction(keyOnRow('Delete')), { kind: 'none' });
	assert.deepEqual(kb.trackTableKeyAction(keyOnRow('Enter', { isComposing: true })), { kind: 'none' });
});

// ------------------------------------------------------------ index math

test('next index: Up/Down move by one and clamp at both ends', () => {
	const n = (current, nav) => kb.nextTrackTableIndex({ current, nav, rowCount: 10, pageSize: 4 });
	assert.equal(n(3, 'down'), 4);
	assert.equal(n(3, 'up'), 2);
	assert.equal(n(9, 'down'), 9);
	assert.equal(n(0, 'up'), 0);
});

test('next index: Home/End jump to the first and last row', () => {
	const n = (current, nav) => kb.nextTrackTableIndex({ current, nav, rowCount: 10, pageSize: 4 });
	assert.equal(n(5, 'home'), 0);
	assert.equal(n(5, 'end'), 9);
	assert.equal(n(-1, 'end'), 9);
});

test('next index: PageUp/PageDown move by the page size and clamp', () => {
	const n = (current, nav) => kb.nextTrackTableIndex({ current, nav, rowCount: 10, pageSize: 4 });
	assert.equal(n(1, 'pagedown'), 5);
	assert.equal(n(7, 'pagedown'), 9);
	assert.equal(n(6, 'pageup'), 2);
	assert.equal(n(2, 'pageup'), 0);
});

test('next index: with no active row the first key lands on an end row', () => {
	const n = (nav) => kb.nextTrackTableIndex({ current: -1, nav, rowCount: 10, pageSize: 4 });
	assert.equal(n('down'), 0);
	assert.equal(n('pagedown'), 0);
	assert.equal(n('up'), 9);
	assert.equal(n('pageup'), 9);
	// A stale index past a shrunken list is treated as no active row.
	assert.equal(kb.nextTrackTableIndex({ current: 40, nav: 'down', rowCount: 10, pageSize: 4 }), 0);
});

test('next index: empty list has nowhere to go', () => {
	assert.equal(kb.nextTrackTableIndex({ current: 0, nav: 'down', rowCount: 0, pageSize: 4 }), -1);
});

test('page size is the viewport row capacity, never below one', () => {
	assert.equal(kb.trackTablePageSize(17), 17);
	assert.equal(kb.trackTablePageSize(17.9), 17);
	assert.equal(kb.trackTablePageSize(0), 1);
	assert.equal(kb.trackTablePageSize(-3), 1);
	assert.equal(kb.trackTablePageSize(Number.NaN), 1);
});

test('range span runs from the fixed anchor to the target in either direction', () => {
	assert.deepEqual(kb.trackTableRangeSpan({ anchor: 3, target: 6, rowCount: 10 }), { from: 3, to: 6 });
	assert.deepEqual(kb.trackTableRangeSpan({ anchor: 6, target: 3, rowCount: 10 }), { from: 3, to: 6 });
	assert.deepEqual(kb.trackTableRangeSpan({ anchor: -1, target: 3, rowCount: 10 }), { from: 3, to: 3 });
	assert.deepEqual(kb.trackTableRangeSpan({ anchor: 12, target: 3, rowCount: 10 }), { from: 3, to: 3 });
});

test('reveal: a visible row does not scroll; rows above/below scroll minimally', () => {
	const base = { rowHeight: 22, headerOffsetPx: 20, viewportHeight: 240 };
	// Rows 0..9 fully visible at scrollTop 0 (20 + 10*22 = 240).
	assert.equal(kb.scrollTopToRevealRow({ ...base, rowIndex: 4, scrollTop: 0 }), 0);
	assert.equal(kb.scrollTopToRevealRow({ ...base, rowIndex: 9, scrollTop: 0 }), 0);
	// Row 10 below the fold: its bottom (20 + 11*22 = 262) aligns to the viewport bottom.
	assert.equal(kb.scrollTopToRevealRow({ ...base, rowIndex: 10, scrollTop: 0 }), 22);
	// Row 2 above the band at scrollTop 100: its top sits just under the sticky header.
	assert.equal(kb.scrollTopToRevealRow({ ...base, rowIndex: 2, scrollTop: 100 }), 44);
	// End of a long list.
	assert.equal(kb.scrollTopToRevealRow({ ...base, rowIndex: 499, scrollTop: 0 }), 20 + 500 * 22 - 240);
});

test('active row: the moved/clicked row wins, else the lead selected row', () => {
	const rows = [
		{ stable_id: 'a', order: 1 },
		{ stable_id: 'b', order: 2 },
		{ stable_id: 'a', order: 3 }
	];
	assert.equal(kb.resolveActiveRowIndex({ rows, activeKey: 'a:3', selectedOrders: [1] }), 2);
	assert.equal(kb.resolveActiveRowIndex({ rows, activeKey: 'gone:9', selectedOrders: [1, 2] }), 1);
	assert.equal(kb.resolveActiveRowIndex({ rows, activeKey: null, selectedOrders: [] }), -1);
	assert.equal(kb.trackRowKey(rows[2]), 'a:3');
});

// ------------------------------------- integration with the pane selection model

/** Drive applySelect exactly as TrackTable's _keyboardMove drives onselectrow
 * (plain move = plain select; Shift move = plain select of the anchor, then a
 * Shift-range select of the target). */
function keyboardShiftDownTwice(reseatAnchor) {
	const rows = Array.from({ length: 8 }, (_, i) => ({ stable_id: `t${i}`, order: i + 1 }));
	const pane = { rows, selected_id: null, selected_ids: [], selected_order: null, selected_orders: [] };
	const plain = (i) => sel.applySelect(pane, rows[i].stable_id, false, false, rows, rows[i].order);
	const shift = (i) => sel.applySelect(pane, rows[i].stable_id, false, true, rows, rows[i].order);
	plain(2); // arrow onto row 2: anchor = 2
	for (const target of [3, 4]) {
		if (reseatAnchor) plain(2);
		shift(target);
	}
	return pane.selected_orders;
}

test('Shift+Down twice through the real selection model selects anchor..target', () => {
	assert.deepEqual(keyboardShiftDownTwice(true), [3, 4, 5]);
});

test('control: without re-seating the anchor the pane range would only span two rows', () => {
	// Proves the plain-then-Shift pair is load-bearing, not decoration.
	assert.deepEqual(keyboardShiftDownTwice(false), [4, 5]);
});

// ------------------------------------------------------------ wiring

test('TrackTable routes row keydown through the keyboard helper', async () => {
	const src = await readFile(TABLE, 'utf8');
	assert.match(src, /from '\.\/track-table-keyboard'/);
	assert.match(src, /const action = trackTableKeyAction\(event\);/);
	assert.match(src, /_keyboardMove\(action\.nav, action\.extend, event\)/);
	assert.match(src, /onkeydown=\{\(e\) => onTrackKeydown\(e, row\)\}/);
});

test('Enter and double-click share one load+play path', async () => {
	const src = await readFile(TABLE, 'utf8');
	const dbl = src.slice(src.indexOf('function onRowDblClick'), src.indexOf('function _requestLoadPlay'));
	assert.match(dbl, /_requestLoadPlay\(row, \{[\s\S]*fromKeyboard: false/);
	const key = src.slice(src.indexOf('function onTrackKeydown'), src.indexOf('// ----- keyboard navigation'));
	assert.match(key, /action\.kind === 'load'[\s\S]*_requestLoadPlay\(row, \{[\s\S]*fromKeyboard: true/);
	const shared = src.slice(src.indexOf('function _requestLoadPlay'), src.indexOf('function hl('));
	assert.match(shared, /onpickdoubledeck\?\.\(row/);
	assert.match(shared, /uiPrefs\.confirm\.dblclick_load_play === false/);
	assert.match(shared, /onloadrow\(/);
});

test('keyboard-opened load confirm focuses Yes so a second Enter confirms', async () => {
	const src = await readFile(TABLE, 'utf8');
	assert.match(src, /if \(loadConfirm\.fromKeyboard\) \{\s*loadConfirmEl\.querySelector<HTMLButtonElement>\('\.load-confirm-yes'\)\?\.focus\(\);/);
});

test('ARIA: grid with multiselect, full rowcount, per-row index/selected, roving tabindex', async () => {
	const src = await readFile(TABLE, 'utf8');
	const table = src.match(/<table\s+data-testid="track-table"[\s\S]*?>/)[0];
	assert.match(table, /role="grid"/);
	assert.match(table, /aria-multiselectable="true"/);
	assert.match(table, /aria-rowcount=\{rows\.length \+ 1\}/);
	assert.match(src, /<thead bind:clientHeight=\{theadHeightPx\}>\s*<tr aria-rowindex=\{1\}>/);
	const row = src.match(/data-testid="track-row"[\s\S]*?draggable="true"/)[0];
	assert.match(row, /data-row-index=\{rowIndex\}/);
	assert.match(row, /aria-rowindex=\{rowIndex \+ 2\}/);
	assert.match(row, /aria-selected=\{selectedOrderSet\.has\(row\.order\)\}/);
	assert.match(row, /tabindex=\{rowIndex === activeRowIndex/);
	assert.doesNotMatch(row, /tabindex="0"/, 'every row a Tab stop defeats roving tabindex');
	assert.match(src, /tbody tr:focus-visible \{/);
});

test('moving the active row scrolls the virtual window, then focuses the mounted row', async () => {
	const src = await readFile(TABLE, 'utf8');
	const fn = src.slice(src.indexOf('function _revealAndFocusRow'), src.indexOf('// ----- AUTOPLAY-COL helpers'));
	assert.match(fn, /scrollTopToRevealRow\(/);
	assert.match(fn, /el\.scrollTop = next;/);
	assert.match(fn, /onscrollcursor\(next\)/);
	assert.match(fn, /await|tick\(\)\.then/);
	assert.match(fn, /tr\[data-row-index="\$\{index\}"\]/);
});

test('BrowserPanel selectRow accepts the keyboard gesture the table passes', async () => {
	const src = await readFile(PANEL, 'utf8');
	assert.match(src, /function selectRow\([\s\S]*?event\?: MouseEvent \| KeyboardEvent\s*\): void/);
	assert.match(src, /onselectrow=\{selectRow\}/);
});
