import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LIBUX-13803: the track table's Date Added column (rekordbox parity, issue #9733).
 *
 * [if] Date Added is missing from the chooser or not hidden by default [then] the column cannot be found or shows uninvited - broken.
 * [if] the first Date Added click does not sort newest first [then] it does not match rekordbox - broken.
 * [if] tracks added the same day do not read title A to Z, then stay stable [then] the order jumps - broken.
 * [if] a row with no added day renders null or undefined [then] the cell shows a placeholder word - broken.
 */

let cols;
let sort;
let pane;
let cell;
let wire;
let tips;
let captured;

before(async () => {
	cols = await loadTypeScriptModule('src/lib/rb/library-columns.ts');
	sort = await loadTypeScriptModule('src/lib/components/rb/browser/browser-sort-ipc.ts');
	pane = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	cell = await loadTypeScriptModule('src/lib/rb/date-added-cell.ts');
	wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
	tips = await loadTypeScriptModule('src/lib/rb/column-tips.ts');
	const fixture = fileURLToPath(
		new URL('./fixtures/playlist-pending-availability-captured.json', import.meta.url)
	);
	captured = JSON.parse(readFileSync(fixture, 'utf8')).find(
		(r) => r.file_availability === 'present'
	);
});

describe('library Date Added column', () => {
	it('is in the chooser, labelled Date Added, hidden by default, with a tip', () => {
		assert.ok(cols.isHideableColumn('date_added'));
		assert.equal(cols.HIDEABLE_COLUMN_LABELS.date_added, 'Date Added');
		assert.ok(cols.DEFAULT_HIDDEN_LIBRARY_COLUMNS.includes('date_added'));
		assert.ok(tips.COLUMN_TIPS.date_added.length > 20);
	});

	it('first click sorts newest first, then oldest first, then clears', () => {
		assert.ok(sort.BROWSER_SORT_KEYS.includes('date_added'));
		assert.equal(sort.firstSortDir('date_added'), -1);
		assert.equal(sort.firstSortDir('title'), 1);
		const store = new pane.PaneStore();
		store.toggleSort('date_added');
		assert.deepEqual([store.sort_key, store.sort_dir], ['date_added', -1]);
		store.toggleSort('date_added');
		assert.deepEqual([store.sort_key, store.sort_dir], ['date_added', 1]);
		store.toggleSort('date_added');
		assert.equal(store.sort_key, null);
		store.toggleSort('title');
		assert.deepEqual([store.sort_key, store.sort_dir], ['title', 1]);
	});

	it('orders equal days by title A to Z, then membership order, undated rows last', () => {
		const rows = [
			{ stable_id: 'old', title: 'Aria', date_added: '2024-03-03' },
			{ stable_id: 'none', title: 'Alpha', date_added: null },
			{ stable_id: 'new-z', title: 'Zeta', date_added: '2026-11-13' },
			{ stable_id: 'new-a', title: 'Echo', date_added: '2026-11-13' },
			{ stable_id: 'new-a-2', title: 'Echo', date_added: '2026-11-13' },
			{ stable_id: 'blank', title: 'Beta', date_added: '' }
		];
		const ids = (dir) => pane.sortRows(rows, 'date_added', dir).map((r) => r.stable_id);
		assert.deepEqual(ids(-1).slice(0, 4), ['new-a', 'new-a-2', 'new-z', 'old']);
		assert.deepEqual(ids(1).slice(0, 4), ['old', 'new-a', 'new-a-2', 'new-z']);
		for (const dir of [1, -1]) assert.deepEqual(ids(dir).slice(4), ['none', 'blank']);
	});

	it('renders the YYYY-MM-DD day, or an empty cell when the row has none', () => {
		assert.equal(cell.dateAddedCellText({ date_added: '2026-11-13' }), '2026-11-13');
		assert.equal(cell.dateAddedCellText({ date_added: null }), '');
		assert.equal(cell.dateAddedCellText({}), '');
	});

	it('keeps date_added from playlist and collection wire rows', () => {
		const playlist = wire.rowFromPlaylistWire({ ...captured, date_added: '2024-03-03' }, 1);
		assert.equal(playlist.date_added, '2024-03-03');
		const { is_streaming: _drop, comments: _comments, ...rest } = captured;
		const listed = wire.rowFromListWire({ ...rest, notes: null, date_added: null }, 1);
		assert.equal(listed.date_added, null);
	});
});
