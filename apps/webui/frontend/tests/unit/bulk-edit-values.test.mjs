/**
 * Issue #2456 / LIBM-63 / FLOW-14: Bulk Edit shows selection consensus.
 *
 * [if] a multi-row selection with a shared rating/notes value is opened in Bulk Edit
 *   [then] that shared value is shown, not a hardcoded default
 * [if] a multi-row selection has mixed values for a field
 *   [then] the modal shows "Multiple," never a fabricated single value
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const mod = await loadTypeScriptModule('src/lib/rb/bulk-edit-values.ts');
const { MIXED_READOUT, bulkEditFieldValues, bulkEditPayload, initialFieldText } = mod;

function row(stable_id, rating, comments) {
	return { stable_id, rating, comments };
}

test('MIXED_READOUT is Multiple', () => {
	assert.equal(MIXED_READOUT, 'Multiple');
});

test('two rows with shared rating 4 and comments warmup', () => {
	const ids = ['a', 'b'];
	const rows = [row('a', 4, 'warmup'), row('b', 4, 'warmup')];
	const { rating, comments } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'shared', value: 4 });
	assert.deepEqual(comments, { kind: 'shared', value: 'warmup' });
});

test('mixed ratings 4 and 5 with shared comments', () => {
	const ids = ['a', 'b'];
	const rows = [row('a', 4, 'warmup'), row('b', 5, 'warmup')];
	const { rating, comments } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'mixed' });
	assert.deepEqual(comments, { kind: 'shared', value: 'warmup' });
});

test('POPM 153 and canonical 3 are shared rating 3 (issue #2572)', () => {
	const ids = ['a', 'b'];
	const rows = [row('a', 153, ''), row('b', 3, '')];
	const { rating } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'shared', value: 3 });
});

test('mixed comments a and b with shared rating', () => {
	const ids = ['a', 'b'];
	const rows = [row('a', 4, 'a'), row('b', 4, 'b')];
	const { rating, comments } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'shared', value: 4 });
	assert.deepEqual(comments, { kind: 'mixed' });
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
	const { comments } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(comments, { kind: 'shared', value: '' });
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
	const { rating, comments } = bulkEditFieldValues(ids, rows);
	assert.deepEqual(rating, { kind: 'shared', value: 2 });
	assert.deepEqual(comments, { kind: 'shared', value: 'note' });
});

test('empty stableIds yields shared null rating and blank comments', () => {
	const { rating, comments } = bulkEditFieldValues([], []);
	assert.deepEqual(rating, { kind: 'shared', value: null });
	assert.deepEqual(comments, { kind: 'shared', value: '' });
});

// Issue #12311 (LIBM-63): mixed values are a read-only readout and never reach the PATCH body.

function draft(overrides = {}) {
	const blank = { set: false, text: '' };
	return {
		stableIds: ['a', 'b'],
		etags: { a: 'ea', b: 'eb' },
		rating: blank,
		notes: blank,
		genre: blank,
		comments: blank,
		tagsAdd: '',
		tagsRemove: '',
		...overrides
	};
}

test('different ratings start as a blank input behind the Multiple readout', () => {
	const { rating } = bulkEditFieldValues(['a', 'b'], [row('a', 4, ''), row('b', 5, '')]);
	assert.deepEqual(rating, { kind: 'mixed' });
	assert.equal(initialFieldText(rating, String), '');
	assert.notEqual(initialFieldText(rating, String), MIXED_READOUT);
});

test('the same comments value is shared and prefills the comments input', () => {
	const { comments } = bulkEditFieldValues(['a', 'b'], [row('a', 4, 'peak'), row('b', 5, 'peak')]);
	assert.deepEqual(comments, { kind: 'shared', value: 'peak' });
	assert.equal(
		initialFieldText(comments, (value) => value),
		'peak'
	);
});

// Real browser rows carry no `notes` key: rowFromListWire maps the track's notes onto
// `comments` (BrowserRow has no notes field). Rows built that way must still read mixed
// Notes as "Multiple" (LIBM-63, issue #12311). The rows come from a captured real server
// response (see browser-row-availability.test.mjs), only their notes are varied here.
const captured = JSON.parse(
	readFileSync(
		fileURLToPath(
			new URL('./fixtures/playlist-pending-availability-captured.json', import.meta.url)
		),
		'utf8'
	)
);

async function browserRows(notesById) {
	const wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
	return Object.entries(notesById).map(([id, notes], i) =>
		wire.rowFromListWire({ ...captured[i], stable_id: id, rating: 4, notes }, i + 1)
	);
}

test('mixed notes on real browser rows read Multiple', async () => {
	const rows = await browserRows({ a: 'n1', b: 'n2' });
	assert.equal('notes' in rows[0], false);
	const { notes } = bulkEditFieldValues(['a', 'b'], rows);
	assert.deepEqual(notes, { kind: 'mixed' });
	assert.equal(
		initialFieldText(notes, (value) => value),
		''
	);
});

test('shared notes on real browser rows prefill the notes input', async () => {
	const rows = await browserRows({ a: 'warmup', b: 'warmup' });
	const { notes } = bulkEditFieldValues(['a', 'b'], rows);
	assert.deepEqual(notes, { kind: 'shared', value: 'warmup' });
});

test('a mixed field left unchanged is absent from the request payload', () => {
	const result = bulkEditPayload(draft({ notes: { set: true, text: 'warmup' } }));
	assert.equal(result.ok, true);
	assert.deepEqual(result.patch, {
		stable_ids: ['a', 'b'],
		expected_etags: { a: 'ea', b: 'eb' },
		notes: 'warmup'
	});
	assert.equal('rating' in result.patch, false);
	assert.equal('comments' in result.patch, false);
	assert.equal(Object.values(result.patch).includes(MIXED_READOUT), false);
});

test('a replacement entered for a mixed field is sent as that value', () => {
	const result = bulkEditPayload(
		draft({ rating: { set: true, text: '5' }, comments: { set: true, text: 'closer' } })
	);
	assert.equal(result.ok, true);
	assert.equal(result.patch.rating, 5);
	assert.equal(result.patch.comments, 'closer');
});

test('nothing ticked and a blank rating are refused before any request', () => {
	assert.deepEqual(bulkEditPayload(draft()), {
		ok: false,
		error: 'set at least one field to apply'
	});
	assert.deepEqual(bulkEditPayload(draft({ rating: { set: true, text: ' ' } })), {
		ok: false,
		error: 'enter a valid rating to apply'
	});
});
