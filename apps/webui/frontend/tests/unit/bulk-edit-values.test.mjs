/**
 * Issue #2456 / LIBM-63 / FLOW-14: Bulk Edit shows selection consensus.
 *
 * [if] a multi-row selection with a shared rating/notes value is opened in Bulk Edit
 *   [then] that shared value is shown, not a hardcoded default
 * [if] a multi-row selection has mixed values for a field
 *   [then] the modal shows "Multiple," never a fabricated single value
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const mod = await loadTypeScriptModule('src/lib/rb/bulk-edit-values.ts');
const { MIXED_READOUT, bulkEditFieldValues } = mod;

function row(stable_id, rating, comments) {
	return { stable_id, rating, comments };
}

test('MIXED_READOUT is Multiple', () => {
	assert.equal(MIXED_READOUT, 'Multiple');
});

test('two rows with shared rating 4 and notes warmup', () => {
	const ids = ['a', 'b'];
	const rows = [row('a', 4, 'warmup'), row('b', 4, 'warmup')];
	const { rating, notes } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'shared', value: 4 });
	assert.deepEqual(notes, { kind: 'shared', value: 'warmup' });
});

test('mixed ratings 4 and 5 with shared notes', () => {
	const ids = ['a', 'b'];
	const rows = [row('a', 4, 'warmup'), row('b', 5, 'warmup')];
	const { rating, notes } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'mixed' });
	assert.deepEqual(notes, { kind: 'shared', value: 'warmup' });
});

test('mixed notes a and b with shared rating', () => {
	const ids = ['a', 'b'];
	const rows = [row('a', 4, 'a'), row('b', 4, 'b')];
	const { rating, notes } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'shared', value: 4 });
	assert.deepEqual(notes, { kind: 'mixed' });
});

test('all ratings null is shared null, not 3', () => {
	const ids = ['a', 'b'];
	const rows = [row('a', null, ''), row('b', null, '')];
	const { rating } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'shared', value: null });
	assert.notEqual(rating.value, 3);
});

test('ratings 0 and null are mixed', () => {
	const ids = ['a', 'b'];
	const rows = [row('a', 0, ''), row('b', null, '')];
	const { rating } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'mixed' });
});

test('comments null and empty string are shared blank', () => {
	const ids = ['a', 'b'];
	const rows = [row('a', null, null), row('b', null, '')];
	const { notes } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(notes, { kind: 'shared', value: '' });
});

test('missing row id makes rating mixed against present row', () => {
	const ids = ['a', 'missing'];
	const rows = [row('a', 4, '')];
	const { rating } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'mixed' });
});

test('single row returns shared values', () => {
	const ids = ['a'];
	const rows = [row('a', 2, 'note')];
	const { rating, notes } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'shared', value: 2 });
	assert.deepEqual(notes, { kind: 'shared', value: 'note' });
});

test('empty stableIds yields shared null rating and blank notes', () => {
	const { rating, notes } = bulkEditFieldValues([], []);
	assert.deepEqual(rating, { kind: 'shared', value: null });
	assert.deepEqual(notes, { kind: 'shared', value: '' });
});
