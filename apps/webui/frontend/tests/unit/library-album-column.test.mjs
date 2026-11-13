import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LIBUX-13747: the track table's Album column (rekordbox parity, issue #9732).
 *
 * [if] Album is missing from the chooser or not hidden by default [then] the column cannot be found or shows uninvited - broken.
 * [if] Album does not sort A to Z then Z to A with empty albums last [then] it does not sort like Artist - broken.
 * [if] a row with no album renders null or undefined [then] the cell shows a placeholder word - broken.
 * [if] a playlist or collection wire row drops album [then] the column is blank for every row - broken.
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
	cell = await loadTypeScriptModule('src/lib/rb/album-cell.ts');
	wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
	tips = await loadTypeScriptModule('src/lib/rb/column-tips.ts');
	const fixture = fileURLToPath(
		new URL('./fixtures/playlist-pending-availability-captured.json', import.meta.url)
	);
	captured = JSON.parse(readFileSync(fixture, 'utf8')).find(
		(r) => r.file_availability === 'present'
	);
});

describe('library Album column', () => {
	it('sits after Artist in the chooser, labelled Album, hidden by default', () => {
		const at = cols.HIDEABLE_COLUMNS.indexOf('album');
		assert.equal(cols.HIDEABLE_COLUMNS[at - 1], 'artist');
		assert.equal(cols.HIDEABLE_COLUMN_LABELS.album, 'Album');
		assert.ok(cols.DEFAULT_HIDDEN_LIBRARY_COLUMNS.includes('album'));
		assert.ok(tips.COLUMN_TIPS.album.length > 20);
	});

	it('is a browser sort key, so the sort IPC accepts it', () => {
		assert.ok(sort.BROWSER_SORT_KEYS.includes('album'));
	});

	it('sorts A to Z then Z to A, empty and missing albums last both ways', () => {
		const rows = [
			{ stable_id: 'none', album: null },
			{ stable_id: 'zeta', album: 'Zeta' },
			{ stable_id: 'blank', album: '  ' },
			{ stable_id: 'alpha', album: 'Alpha' },
			{ stable_id: 'absent' }
		];
		const ids = (dir) => pane.sortRows(rows, 'album', dir).map((r) => r.stable_id);
		assert.deepEqual(ids(1).slice(0, 2), ['alpha', 'zeta']);
		assert.deepEqual(ids(-1).slice(0, 2), ['zeta', 'alpha']);
		for (const dir of [1, -1])
			assert.deepEqual(new Set(ids(dir).slice(2)), new Set(['none', 'blank', 'absent']));
	});

	it('renders an empty cell for a row with no album, never null or undefined', () => {
		assert.equal(cell.albumCellText({ album: null }), '');
		assert.equal(cell.albumCellText({}), '');
		assert.equal(cell.albumCellText({ album: 'Harbor EP' }), 'Harbor EP');
	});

	it('keeps album from playlist and collection wire rows', () => {
		const playlist = wire.rowFromPlaylistWire({ ...captured, album: 'Harbor EP' }, 1);
		assert.equal(playlist.album, 'Harbor EP');
		const { is_streaming: _drop, comments: _comments, ...rest } = captured;
		const listed = wire.rowFromListWire({ ...rest, notes: null, album: null }, 1);
		assert.equal(listed.album, null);
	});
});
