import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LIBUX-13824: the track table's Bitrate and Kind columns (rekordbox parity, issue #9735).
 *
 * [if] Bitrate does not render `<n> kbps`, or shows text when kbps is null [then] the cell lies - broken.
 * [if] Bitrate sorts as text (1000 before 320) [then] a crate cannot be sorted by bitrate - broken.
 * [if] Kind is not the upper-case container or does not sort A to Z [then] it does not match rekordbox - broken.
 * [if] either column is missing from the chooser or not hidden by default [then] it shows uninvited - broken.
 */

let cols;
let sort;
let pane;
let cell;
let tips;

const q = (kbps, container) => ({ kbps, container });

before(async () => {
	cols = await loadTypeScriptModule('src/lib/rb/library-columns.ts');
	sort = await loadTypeScriptModule('src/lib/components/rb/browser/browser-sort-ipc.ts');
	pane = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	cell = await loadTypeScriptModule('src/lib/rb/bitrate-kind-cells.ts');
	tips = await loadTypeScriptModule('src/lib/rb/column-tips.ts');
});

describe('library Bitrate and Kind columns', () => {
	it('renders Bitrate as <n> kbps, empty when kbps is null', () => {
		assert.equal(cell.bitrateCellText({ quality: q(320, '.mp3') }), '320 kbps');
		assert.equal(cell.bitrateCellText({ quality: q(1411, '.wav') }), '1411 kbps');
		assert.equal(cell.bitrateCellText({ quality: q(null, '.mp3') }), '');
		assert.equal(cell.bitrateCellText({ quality: null }), '');
		assert.equal(cell.bitrateCellText({}), '');
	});

	it('sorts Bitrate numerically, unknown rates last both ways', () => {
		const rows = [
			{ stable_id: 'r320', title: 'A', quality: q(320, '.mp3') },
			{ stable_id: 'none', title: 'B', quality: q(null, '.mp3') },
			{ stable_id: 'r1411', title: 'C', quality: q(1411, '.wav') },
			{ stable_id: 'r96', title: 'D', quality: q(96, '.m4a') }
		];
		const ids = (dir) => pane.sortRows(rows, 'bitrate', dir).map((r) => r.stable_id);
		assert.deepEqual(ids(1), ['r96', 'r320', 'r1411', 'none']);
		assert.deepEqual(ids(-1), ['r1411', 'r320', 'r96', 'none']);
	});

	it('renders Kind as the upper-case container and sorts it A to Z, empty last', () => {
		assert.equal(cell.kindCellText({ quality: q(320, '.mp3') }), 'MP3');
		assert.equal(cell.kindCellText({ quality: q(null, '.flac') }), 'FLAC');
		assert.equal(cell.kindCellText({ quality: q(null, '') }), '');
		assert.equal(cell.kindCellText({}), '');
		const rows = [
			{ stable_id: 'wav', title: 'A', quality: q(1411, '.wav') },
			{ stable_id: 'none', title: 'B', quality: q(null, '') },
			{ stable_id: 'aiff', title: 'C', quality: q(1411, '.aiff') },
			{ stable_id: 'mp3', title: 'D', quality: q(320, '.mp3') },
			{ stable_id: 'flac', title: 'E', quality: q(900, '.flac') }
		];
		const ids = (dir) => pane.sortRows(rows, 'kind', dir).map((r) => r.stable_id);
		assert.deepEqual(ids(1), ['aiff', 'flac', 'mp3', 'wav', 'none']);
		assert.deepEqual(ids(-1), ['wav', 'mp3', 'flac', 'aiff', 'none']);
	});

	it('lists both in the chooser, hidden by default, with tips and sort keys', () => {
		for (const [id, label] of [
			['bitrate', 'Bitrate'],
			['kind', 'Kind']
		]) {
			assert.ok(cols.isHideableColumn(id), id);
			assert.equal(cols.HIDEABLE_COLUMN_LABELS[id], label);
			assert.ok(cols.DEFAULT_HIDDEN_LIBRARY_COLUMNS.includes(id), id);
			assert.ok(tips.COLUMN_TIPS[id].length > 20, id);
			assert.ok(sort.BROWSER_SORT_KEYS.includes(id), id);
			assert.equal(sort.firstSortDir(id), 1, id);
		}
	});
});
