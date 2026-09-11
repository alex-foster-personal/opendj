import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// All Tracks first-page paint helpers (issue #318). Regression lines:
// - if publishFirstPage does not clear loading while leaving load_progress
//   intact, then the spinner still hides the table (or the overlay vanishes
//   too early)
// - if a stale seq appendRows mutates a newer load's rows, then broken
// - if appendRows / publishFirstPage reset selected_id or scroll_top, then
//   a mid-fill selection or scroll jumps
// - if finishFill leaves load_progress set, then a finished pane keeps
//   showing a loading bar
// - if finishFill(..., true) does not set truncated, then a stalled fill
//   looks complete

let contract;
let progressive;

before(async () => {
	contract = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-contract.svelte.ts'
	);
	progressive = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-progressive-load.ts'
	);
});

function _row(overrides = {}) {
	return {
		stable_id: 'sid-default',
		order: 1,
		title: null,
		artist: null,
		...overrides
	};
}

test('publishFirstPage clears loading and leaves load_progress intact', () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	p.updateLoadProgress(seq, 2, 10);
	const rows = [_row({ stable_id: 'a' }), _row({ stable_id: 'b', order: 2 })];

	assert.equal(progressive.publishFirstPage(p, seq, rows), true);
	assert.equal(p.loading, false);
	assert.equal(p.rows.length, 2);
	assert.equal(p.truncated, false);
	assert.equal(p.etag, '');
	assert.deepEqual(p.load_progress, { loaded: 2, total: 10 });
});

test('publishFirstPage and appendRows do not reset selected_id or scroll_top', () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	assert.equal(progressive.publishFirstPage(p, seq, [_row({ stable_id: 'a' })]), true);
	p.select('a');
	p.rememberScroll(88);

	assert.equal(progressive.publishFirstPage(p, seq, [_row({ stable_id: 'a' }), _row({ stable_id: 'b', order: 2 })]), true);
	assert.equal(p.selected_id, 'a');
	assert.equal(p.scroll_top, 88);

	assert.equal(progressive.appendRows(p, seq, [_row({ stable_id: 'c', order: 3 })]), true);
	assert.equal(p.selected_id, 'a');
	assert.deepEqual(p.selected_ids, ['a']);
	assert.equal(p.scroll_top, 88);
	assert.equal(p.loading, false);
});

test('stale seq appendRows does not mutate a newer load\'s rows', () => {
	const p = contract.createPaneStore();
	const stale = p.beginLoad('all', 'All Tracks');
	assert.equal(progressive.publishFirstPage(p, stale, [_row({ stable_id: 'old' })]), true);

	const fresh = p.beginLoad('pl-1', 'Warmup');
	assert.deepEqual(p.rows, []);
	assert.equal(p.loading, true);

	assert.equal(progressive.appendRows(p, stale, [_row({ stable_id: 'late' })]), false);
	assert.deepEqual(p.rows, []);
	assert.equal(p.loading, true);
	assert.equal(progressive.publishFirstPage(p, stale, [_row({ stable_id: 'late' })]), false);
	assert.deepEqual(p.rows, []);
	assert.equal(p.isCurrentLoad(fresh), true);
});

test('appendRows no-ops on an empty extra page', () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	const first = [_row({ stable_id: 'a' })];
	assert.equal(progressive.publishFirstPage(p, seq, first), true);
	const before = p.rows;
	assert.equal(progressive.appendRows(p, seq, []), false);
	assert.equal(p.rows, before);
	assert.equal(p.rows.length, 1);
});

test('finishFill clears load_progress so a finished pane does not keep a loading bar', () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	assert.equal(progressive.publishFirstPage(p, seq, [_row({ stable_id: 'a' })]), true);
	p.updateLoadProgress(seq, 1, 10);

	assert.equal(progressive.finishFill(p, seq, false), true);
	assert.equal(p.loading, false);
	assert.equal(p.load_progress, null);
	assert.equal(p.truncated, false);
	assert.equal(p.rows.length, 1);
});

test('finishFill(..., true) sets truncated so a stalled fill does not look complete', () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	assert.equal(progressive.publishFirstPage(p, seq, [_row({ stable_id: 'a' })]), true);
	p.updateLoadProgress(seq, 1, 10);

	assert.equal(progressive.finishFill(p, seq, true), true);
	assert.equal(p.truncated, true);
	assert.equal(p.load_progress, null);
	assert.equal(p.loading, false);
	assert.equal(p.rows.length, 1);
	assert.equal(p.error, null);
});

test('stale finishFill does not clear a newer load', () => {
	const p = contract.createPaneStore();
	const stale = p.beginLoad('all', 'All Tracks');
	const fresh = p.beginLoad('pl-1', 'Warmup');
	p.updateLoadProgress(fresh, 3, 9);

	assert.equal(progressive.finishFill(p, stale, true), false);
	assert.equal(p.loading, true);
	assert.deepEqual(p.load_progress, { loaded: 3, total: 9 });
	assert.equal(p.truncated, false);
});
