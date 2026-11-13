import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LIBUX-13845: the track table's Last Played column (rekordbox parity, issue #9736).
 *
 * [if] Last Played does not show a YYYY-MM-DD day, or shows text for a never-played row [then] the cell lies - broken.
 * [if] the first Last Played click does not sort newest first [then] it does not match rekordbox - broken.
 * [if] the column is missing from the chooser or not hidden by default [then] it shows uninvited - broken.
 * [if] last_played_at is dropped from playlist or collection wire rows [then] the column is always blank - broken.
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
	cell = await loadTypeScriptModule('src/lib/rb/last-played-day-cell.ts');
	wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
	tips = await loadTypeScriptModule('src/lib/rb/column-tips.ts');
	const fixture = fileURLToPath(
		new URL('./fixtures/playlist-pending-availability-captured.json', import.meta.url)
	);
	captured = JSON.parse(readFileSync(fixture, 'utf8')).find(
		(r) => r.file_availability === 'present'
	);
});

/** Local YYYY-MM-DD of an instant, the same rule the cell uses. */
function localDay(iso) {
	const d = new Date(iso);
	const pad = (n) => String(n).padStart(2, '0');
	return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

describe('library Last Played column', () => {
	it('renders the local YYYY-MM-DD day of the last play, empty when never played', () => {
		const at = '2026-11-12T12:00:00.000000+00:00';
		assert.match(cell.lastPlayedCellText({ last_played_at: at }), /^\d{4}-\d{2}-\d{2}$/);
		assert.equal(cell.lastPlayedCellText({ last_played_at: at }), localDay(at));
		assert.equal(cell.lastPlayedCellText({ last_played_at: null }), '');
		assert.equal(cell.lastPlayedCellText({ last_played_at: 'not a date' }), '');
		assert.equal(cell.lastPlayedCellText({}), '');
	});

	it('first click sorts newest first, then oldest first, then clears', () => {
		assert.ok(sort.BROWSER_SORT_KEYS.includes('last_played'));
		assert.equal(sort.firstSortDir('last_played'), -1);
		const store = new pane.PaneStore();
		store.toggleSort('last_played');
		assert.deepEqual([store.sort_key, store.sort_dir], ['last_played', -1]);
		store.toggleSort('last_played');
		assert.deepEqual([store.sort_key, store.sort_dir], ['last_played', 1]);
		store.toggleSort('last_played');
		assert.equal(store.sort_key, null);
	});

	it('orders by the play instant, never-played rows last both ways', () => {
		const rows = [
			{ stable_id: 'old', title: 'A', last_played_at: '2025-01-01T10:00:00+00:00' },
			{ stable_id: 'never', title: 'B', last_played_at: null },
			{ stable_id: 'new', title: 'C', last_played_at: '2026-11-12T21:15:00+00:00' },
			// Same instant as 20:00Z written with an offset: compared as time, not text.
			{ stable_id: 'mid', title: 'D', last_played_at: '2026-11-12T22:00:00+02:00' }
		];
		const ids = (dir) => pane.sortRows(rows, 'last_played', dir).map((r) => r.stable_id);
		assert.deepEqual(ids(-1), ['new', 'mid', 'old', 'never']);
		assert.deepEqual(ids(1), ['old', 'mid', 'new', 'never']);
	});

	it('is in the chooser, labelled Last Played, hidden by default, with a tip', () => {
		assert.ok(cols.isHideableColumn('last_played'));
		assert.equal(cols.HIDEABLE_COLUMN_LABELS.last_played, 'Last Played');
		assert.ok(cols.DEFAULT_HIDDEN_LIBRARY_COLUMNS.includes('last_played'));
		assert.ok(tips.COLUMN_TIPS.last_played.length > 20);
	});

	it('keeps last_played_at from playlist and collection wire rows', () => {
		const at = '2026-11-12T21:15:00.000000+00:00';
		const playlist = wire.rowFromPlaylistWire({ ...captured, last_played_at: at }, 1);
		assert.equal(playlist.last_played_at, at);
		const { is_streaming: _drop, comments: _comments, ...rest } = captured;
		const listed = wire.rowFromListWire({ ...rest, notes: null, last_played_at: null }, 1);
		assert.equal(listed.last_played_at, null);
	});
});
