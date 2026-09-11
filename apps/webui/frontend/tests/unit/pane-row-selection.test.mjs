import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const TRACK_TABLE = `${FRONTEND_ROOT}/src/lib/components/rb/browser/TrackTable.svelte`;

let selection;
let contract;

before(async () => {
	selection = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-row-selection.ts'
	);
	contract = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-contract.svelte.ts'
	);
});

function makePane(rows = []) {
	return {
		rows,
		selected_id: null,
		selected_ids: [],
		selected_order: null,
		selected_orders: []
	};
}

const dupRows = [
	{ stable_id: 'dup', order: 1 },
	{ stable_id: 'other', order: 2 },
	{ stable_id: 'dup', order: 3 }
];

test('clicking one duplicate selects only that order (rb-row-selected)', () => {
	const pane = makePane(dupRows);
	selection.applySelect(pane, 'dup', false, false, [], 3);
	const rbRowSelected = dupRows.map((row) =>
		selection.isSelectedRow(row, pane.selected_orders) ? 'rb-row-selected' : ''
	);
	assert.deepEqual(rbRowSelected, ['', '', 'rb-row-selected']);
	assert.deepEqual(pane.selected_ids, ['dup']);
	assert.equal(pane.selected_id, 'dup');
});

test('clicking the first duplicate selects only order 1', () => {
	const pane = makePane(dupRows);
	selection.applySelect(pane, 'dup', false, false, [], 1);
	const rbRowSelected = dupRows.map((row) =>
		selection.isSelectedRow(row, pane.selected_orders) ? 'rb-row-selected' : ''
	);
	assert.deepEqual(rbRowSelected, ['rb-row-selected', '', '']);
});

test('distinct stable_ids stay independently selectable', () => {
	const rows = [
		{ stable_id: 'file-a', order: 1 },
		{ stable_id: 'file-b', order: 2 }
	];
	const pane = makePane(rows);
	selection.applySelect(pane, 'file-a', false, false, [], 1);
	selection.applySelect(pane, 'file-b', false, false, [], 2);
	assert.deepEqual(pane.selected_orders, [2]);
	assert.deepEqual(pane.selected_ids, ['file-b']);

	selection.applySelect(pane, 'file-a', true, false, [], 1);
	assert.deepEqual(pane.selected_orders, [2, 1]);
	assert.deepEqual(pane.selected_ids, ['file-b', 'file-a']);
});

test('cmd-click two copies of the same stable_id keeps one id', () => {
	const pane = makePane(dupRows);
	selection.applySelect(pane, 'dup', false, false, [], 1);
	selection.applySelect(pane, 'dup', true, false, [], 3);
	assert.deepEqual(pane.selected_orders, [1, 3]);
	assert.deepEqual(pane.selected_ids, ['dup']);
});

test('shift-click range across duplicate copies selects all three orders', () => {
	const pane = makePane(dupRows);
	selection.applySelect(pane, 'dup', false, false, [], 1);
	selection.applySelect(pane, 'dup', false, true, dupRows, 3);
	assert.deepEqual(pane.selected_orders, [1, 2, 3]);
	assert.deepEqual(pane.selected_ids, ['dup', 'other']);
});

test('master highlight uses first matching order only', () => {
	assert.equal(selection.masterHighlightOrder(dupRows, 'dup'), 1);
	const masterOrder = selection.masterHighlightOrder(dupRows, 'dup');
	const rbRowMaster = dupRows.map((row) =>
		masterOrder !== null && row.order === masterOrder ? 'rb-row-master' : ''
	);
	assert.deepEqual(rbRowMaster, ['rb-row-master', '', '']);
});

test('masterHighlightOrder returns null when no master', () => {
	assert.equal(selection.masterHighlightOrder(dupRows, null), null);
});

test('clearSelection wipes positional fields', () => {
	const pane = makePane(dupRows);
	selection.applySelect(pane, 'dup', false, false, [], 3);
	selection.clearSelection(pane);
	assert.equal(pane.selected_id, null);
	assert.deepEqual(pane.selected_ids, []);
	assert.equal(pane.selected_order, null);
	assert.deepEqual(pane.selected_orders, []);
});

test('beginLoad clears selected_orders', () => {
	const p = contract.createPaneStore();
	p.rows = dupRows;
	selection.applySelect(p, 'dup', false, false, [], 3);
	p.beginLoad('pl-2', 'Other');
	assert.equal(p.selected_order, null);
	assert.deepEqual(p.selected_orders, []);
});

test('pruneSelection drops a deleted slot and keeps the other copy', () => {
	const pane = makePane(dupRows);
	selection.applySelect(pane, 'dup', true, false, [], 1);
	selection.applySelect(pane, 'dup', true, false, [], 3);
	const remaining = [{ stable_id: 'other', order: 2 }, { stable_id: 'dup', order: 3 }];
	pane.rows = remaining;
	selection.pruneSelection(pane, remaining);
	assert.deepEqual(pane.selected_orders, [3]);
	assert.deepEqual(pane.selected_ids, ['dup']);
});

test('TrackTable binds row chrome to selectedOrderSet and masterOrder', () => {
	const table = readFileSync(TRACK_TABLE, 'utf8');
	assert.match(table, /class:rb-row-selected=\{selectedOrderSet\.has\(row\.order\)\}/);
	assert.equal(table.includes('class:rb-row-selected={selectedIdSet.has(row.stable_id)}'), false);
	assert.match(table, /class:rb-row-master=\{masterOrder !== null && row\.order === masterOrder\}/);
	assert.equal(
		table.includes('class:rb-row-master={masterStableId !== null && row.stable_id === masterStableId}'),
		false
	);
});
