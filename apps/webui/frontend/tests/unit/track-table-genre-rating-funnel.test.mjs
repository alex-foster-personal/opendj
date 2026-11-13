import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LIBUX-13958: the Genre and Rating column funnels (issue #9740) (rekordbox Track Filter Genre and Rating, mimicked).
 *
 * [if] the Genre funnel does not list the genres of the list with their counts [then] it offers the wrong choices - broken.
 * [if] picking several genres drops a row in one of them, or letter case splits a genre [then] it disagrees with the API - broken.
 * [if] a Rating minimum keeps an unrated or lower-rated row [then] the funnel lies - broken.
 * [if] clearing does not restore every row, or one pane's filter leaks into another [then] it is not per pane - broken.
 */

let filters;
let store;
let explainers;

const ROWS = [
	{ stable_id: 'house-4', genre: 'House', rating: 4 },
	{ stable_id: 'house-lower-5', genre: ' house ', rating: 5 },
	{ stable_id: 'techno-3', genre: 'Techno', rating: 3 },
	{ stable_id: 'trance-unrated', genre: 'Trance', rating: null },
	{ stable_id: 'no-genre-2', genre: null, rating: 2 },
	{ stable_id: 'blank-genre-0', genre: '  ', rating: 0 }
];

const ids = (rows) => rows.map((r) => r.stable_id);
const withFilters = (patch) => ({ ...filters.EMPTY_TRACK_TABLE_COLUMN_FILTERS, ...patch });

before(async () => {
	filters = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-column-filters.ts'
	);
	store = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-funnel-store.svelte.ts'
	);
	explainers = await loadTypeScriptModule('src/lib/rb/planned-explainers.ts');
});

describe('track-table Genre funnel', () => {
	it('lists the genres present in the list with counts, most rows first, blanks left out', () => {
		assert.deepEqual(filters.genreFacets(ROWS), [
			{ value: 'house', label: 'House', count: 2 },
			{ value: 'techno', label: 'Techno', count: 1 },
			{ value: 'trance', label: 'Trance', count: 1 }
		]);
		assert.deepEqual(filters.genreFacets([]), []);
	});

	it('keeps rows in any picked genre, ignoring letter case and spaces', () => {
		let genre = filters.EMPTY_GENRE_FILTER;
		genre = filters.toggleGenreSelection(genre, 'HOUSE');
		genre = filters.toggleGenreSelection(genre, 'techno');
		assert.equal(filters.genreFilterActive(genre), true);
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, withFilters({ genre }))), [
			'house-4',
			'house-lower-5',
			'techno-3'
		]);
		genre = filters.toggleGenreSelection(genre, 'Techno');
		assert.deepEqual(genre.selected, ['house']);
		assert.equal(filters.toggleGenreSelection(genre, '   '), genre);
	});
});

describe('track-table Rating funnel', () => {
	it('offers 1 to 5 stars and hides unrated and lower-rated rows while set', () => {
		const rating = filters.ratingFilterFor(filters.EMPTY_RATING_FILTER, 4);
		assert.deepEqual(rating, { minStars: 4 });
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, withFilters({ rating }))), [
			'house-4',
			'house-lower-5'
		]);
		const one = filters.ratingFilterFor(filters.EMPTY_RATING_FILTER, 1);
		assert.equal(
			ids(filters.applyTrackTableColumnFilters(ROWS, withFilters({ rating: one }))).includes(
				'trance-unrated'
			),
			false
		);
		for (const bad of [0, 6, 2.5, Number.NaN]) {
			assert.equal(filters.ratingFilterFor(rating, bad), filters.EMPTY_RATING_FILTER, String(bad));
		}
		assert.equal(filters.ratingFilterFor(rating, 4), filters.EMPTY_RATING_FILTER);
	});

	it('combines with the Genre funnel', () => {
		const genre = filters.toggleGenreSelection(filters.EMPTY_GENRE_FILTER, 'house');
		const rating = filters.ratingFilterFor(filters.EMPTY_RATING_FILTER, 5);
		assert.deepEqual(
			ids(filters.applyTrackTableColumnFilters(ROWS, withFilters({ genre, rating }))),
			['house-lower-5']
		);
		assert.equal(filters.columnFiltersActiveCount(withFilters({ genre, rating })), 2);
	});
});

describe('Genre and Rating funnels per pane', () => {
	it('clears back to every row and keeps each pane its own filter', () => {
		const s = new store.TrackTableFunnelStore();
		s.patchGenre('pane-a', filters.toggleGenreSelection(filters.EMPTY_GENRE_FILTER, 'Trance'));
		s.patchRating('pane-b', filters.ratingFilterFor(filters.EMPTY_RATING_FILTER, 5));
		assert.deepEqual(s.get('pane-b').genre, filters.EMPTY_GENRE_FILTER);
		assert.deepEqual(s.get('pane-a').rating, filters.EMPTY_RATING_FILTER);
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, s.get('pane-a'))), [
			'trance-unrated'
		]);
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, s.get('pane-b'))), [
			'house-lower-5'
		]);
		s.patchGenre('pane-a', filters.EMPTY_GENRE_FILTER);
		s.patchRating('pane-b', filters.EMPTY_RATING_FILTER);
		assert.equal(filters.applyTrackTableColumnFilters(ROWS, s.get('pane-a')), ROWS);
		assert.equal(filters.applyTrackTableColumnFilters(ROWS, s.get('pane-b')), ROWS);
	});

	it('the planned column-filter stub no longer promises the genre or rating filter', () => {
		const text = explainers.plannedTitle('track-table-filter');
		assert.doesNotMatch(text, /\(genre, rating\)/);
		assert.match(text, /genres and the minimum rating have their own funnels/);
	});
});
