import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Issue #10029: the Year and Label columns read the file's own tags (rekordbox parity).
 *
 * [if] Year or Label is missing from the chooser or shown by default [then] the column cannot be found or shows uninvited - broken.
 * [if] a row's tag year or label is not the cell when no classical value is confirmed [then] the columns stay blank - broken.
 * [if] Year does not sort numerically or Label A to Z, blanks last [then] the sort is wrong - broken.
 * [if] a plain search for a label misses its tracks [then] q=Hotflush finds nothing - broken.
 * [if] a collection or playlist wire row drops year or label [then] the columns are blank for every row - broken.
 */

let works;
let pane;
let search;
let wire;
let sort;
let query;
let captured;

before(async () => {
	works = await loadTypeScriptModule('src/lib/rb/library-works.ts');
	pane = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	search = await loadTypeScriptModule('src/lib/components/rb/browser/track-table-search.ts');
	wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
	sort = await loadTypeScriptModule('src/lib/components/rb/browser/browser-sort-ipc.ts');
	query = await loadTypeScriptModule('src/lib/rb/browser-search-query.ts');
	const fixture = fileURLToPath(
		new URL('./fixtures/playlist-pending-availability-captured.json', import.meta.url)
	);
	captured = JSON.parse(readFileSync(fixture, 'utf8')).find(
		(r) => r.file_availability === 'present'
	);
});

const row = (over) => ({
	stable_id: 'x',
	title: 't',
	artist: 'a',
	comments: null,
	key: null,
	genre: null,
	bpm: null,
	rating: null,
	...over
});

describe('library Year and Label columns from tags', () => {
	it('are in the chooser, off by default, and sort keys the sort IPC accepts', () => {
		assert.ok(works.OPTIONAL_COLUMNS.includes('year'));
		assert.ok(works.OPTIONAL_COLUMNS.includes('label'));
		assert.deepEqual(works.normalizeOptionalColumns([]), []);
		assert.ok(sort.BROWSER_SORT_KEYS.includes('year'));
		assert.ok(sort.BROWSER_SORT_KEYS.includes('label'));
	});

	it('show the tag value, and a confirmed classical value wins over it', () => {
		const overlay = {
			...works.EMPTY_OVERLAY,
			classical: { cls: { label: 'DG', recording_year: 1962 } }
		};
		assert.equal(works.labelOf(overlay, 'tagged', 'Hotflush'), 'Hotflush');
		assert.equal(works.yearOf(overlay, 'tagged', 2019), 2019);
		assert.equal(works.labelOf(overlay, 'cls', 'Hotflush'), 'DG');
		assert.equal(works.yearOf(overlay, 'cls', 2019), 1962);
		assert.equal(works.labelOf(overlay, 'none', null), '');
		assert.equal(works.yearOf(overlay, 'none', null), null);
	});

	it('sort Year numerically and Label A to Z, blanks last both ways', () => {
		const rows = [
			row({ stable_id: 'y2001', year: 2001, label: 'zeta' }),
			row({ stable_id: 'none', year: null, label: null }),
			row({ stable_id: 'y999', year: 999, label: 'Alpha' }),
			row({ stable_id: 'y2019', year: 2019, label: 'Hotflush' })
		];
		const ids = (key, dir) => pane.sortRows(rows, key, dir).map((r) => r.stable_id);
		assert.deepEqual(ids('year', 1), ['y999', 'y2001', 'y2019', 'none']);
		assert.deepEqual(ids('year', -1), ['y2019', 'y2001', 'y999', 'none']);
		assert.deepEqual(ids('label', 1), ['y999', 'y2019', 'y2001', 'none']);
		assert.deepEqual(ids('label', -1), ['y2001', 'y2019', 'y999', 'none']);
	});

	it('let a plain search find a track by its label', () => {
		const rows = [
			row({ stable_id: 'on', label: 'Hotflush' }),
			row({ stable_id: 'off', label: 'Other' })
		];
		const hits = rows.filter((r) =>
			query.matchesSearchQuery(search.searchableTrackFromBrowserRow(r), 'hotflush')
		);
		assert.deepEqual(
			hits.map((r) => r.stable_id),
			['on']
		);
	});

	it('keep year and label from playlist and collection wire rows', () => {
		const tagged = { ...captured, year: 2019, label: 'Hotflush' };
		const playlist = wire.rowFromPlaylistWire(tagged, 1);
		assert.deepEqual([playlist.year, playlist.label], [2019, 'Hotflush']);
		const { is_streaming: _drop, comments: _comments, ...rest } = tagged;
		const listed = wire.rowFromListWire({ ...rest, notes: null }, 1);
		assert.deepEqual([listed.year, listed.label], [2019, 'Hotflush']);
		const old = wire.rowFromListWire(
			{ ...rest, notes: null, year: undefined, label: undefined },
			1
		);
		assert.deepEqual([old.year, old.label], [null, null]);
	});
});
