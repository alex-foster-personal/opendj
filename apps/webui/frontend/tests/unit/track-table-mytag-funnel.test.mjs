import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * The My Tag funnel on the Tags header (issue #9742) (rekordbox Track Filter My Tag, mimicked).
 *
 * [if] picking two tags with OR drops a row holding either [then] OR matching lies - broken.
 * [if] picking two tags with AND keeps a row holding only one [then] AND matching lies - broken.
 * [if] letter case or width splits a tag [then] the funnel disagrees with the tracks API - broken.
 * [if] clearing does not restore every row, or one pane's picks leak into another [then] it is not per pane - broken.
 */

let filters;
let store;

// The same tag sets tests/webui/test_tracks_mytag_filter.py serves, so the UI and the API agree.
const TAGS = {
	'track-001': ['Warmup', 'Vocal'],
	'track-002': ['peak'],
	'track-003': ['WARMUP'],
	'track-004': ['vocal', 'Peak'],
	'track-005': []
};
// "WARMUP" in full-width letters.
const FULL_WIDTH_WARMUP = String.fromCodePoint(0xff37, 0xff21, 0xff32, 0xff2d, 0xff35, 0xff30);
const ROWS = Object.keys(TAGS).map((stable_id) => ({ stable_id }));
const tagsOf = (row) => TAGS[row.stable_id];

const ids = (rows) => rows.map((r) => r.stable_id);
const withMytag = (mytag) => ({ ...filters.EMPTY_TRACK_TABLE_COLUMN_FILTERS, mytag });
const pick = (names, mode) =>
	filters.mytagFilterWithMode(
		names.reduce((f, name) => filters.toggleMytagSelection(f, name), filters.EMPTY_MYTAG_FILTER),
		mode
	);

before(async () => {
	filters = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-column-filters.ts'
	);
	store = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-funnel-store.svelte.ts'
	);
});

describe('track-table My Tag funnel', () => {
	it('with OR keeps every row holding either of two picked tags', () => {
		/** [if] warmup and peak are picked with OR [then] rows 1 to 4 stay and the untagged row goes, [else stop]. */
		const mytag = pick(['warmup', 'peak'], 'or');
		assert.equal(filters.mytagFilterActive(mytag), true);
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, withMytag(mytag), tagsOf)), [
			'track-001',
			'track-002',
			'track-003',
			'track-004'
		]);
	});

	it('with AND keeps only rows holding both picked tags', () => {
		/** [if] vocal and peak are picked with AND [then] only the row holding both stays, [else stop]. */
		const mytag = pick(['vocal', 'peak'], 'and');
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, withMytag(mytag), tagsOf)), [
			'track-004'
		]);
		assert.deepEqual(filters.mytagFilterWithMode(mytag, 'or').selected, mytag.selected);
	});

	it('matches a tag ignoring letter case and width, like the tag catalog', () => {
		/** [if] a pick is full-width upper case [then] it matches Warmup and WARMUP, [else stop]. */
		const mytag = pick([`${FULL_WIDTH_WARMUP} `], 'or');
		assert.deepEqual(mytag.selected, ['warmup']);
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, withMytag(mytag), tagsOf)), [
			'track-001',
			'track-003'
		]);
		assert.equal(filters.toggleMytagSelection(mytag, '   '), mytag);
	});

	it('reads the row tags when no lookup is passed and counts as one active funnel', () => {
		/** [if] rows carry their own tags [then] the default lookup filters them and the count is 1, [else stop]. */
		const rows = [
			{ stable_id: 'a', tags: ['Peak'] },
			{ stable_id: 'b', tags: [] }
		];
		const all = withMytag(pick(['peak'], 'or'));
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(rows, all)), ['a']);
		assert.equal(filters.columnFiltersActiveCount(all), 1);
		assert.match(filters.columnFiltersTitle(all), /My Tag \(or\): peak/);
	});
});

describe('My Tag funnel per pane', () => {
	it('clears back to every row and keeps each pane its own picks', () => {
		/** [if] pane A picks a tag [then] pane B keeps every row, and clearing A restores its rows, [else stop]. */
		const s = new store.TrackTableFunnelStore();
		s.patchMytag('pane-a', pick(['peak'], 'or'));
		assert.deepEqual(s.get('pane-b').mytag, filters.EMPTY_MYTAG_FILTER);
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, s.get('pane-a'), tagsOf)), [
			'track-002',
			'track-004'
		]);
		assert.equal(filters.applyTrackTableColumnFilters(ROWS, s.get('pane-b'), tagsOf), ROWS);
		s.patchMytag('pane-a', filters.EMPTY_MYTAG_FILTER);
		assert.equal(filters.applyTrackTableColumnFilters(ROWS, s.get('pane-a'), tagsOf), ROWS);
	});
});
