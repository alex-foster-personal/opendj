import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LIBUX-13909: the BPM column funnel (issue #9738) (rekordbox Track Filter BPM range, mimicked).
 *
 * [if] a min/max range keeps a row outside it or drops an edge row [then] the funnel is not inclusive - broken.
 * [if] clearing does not restore every row, or one pane's range leaks into another [then] the funnel is not per pane - broken.
 * [if] a sort change loses the range [then] the funnel does not survive sorting - broken.
 * [if] min above max, a negative or junk input is accepted [then] the funnel hides every row silently - broken.
 * [if] the planned stub still promises the BPM range [then] the explainer lies about a shipped funnel - broken.
 */

let filters;
let store;
let pane;
let explainers;

const ROWS = [
	{ stable_id: 'slow', title: 'A', bpm: 92 },
	{ stable_id: 'edge-low', title: 'B', bpm: 120 },
	{ stable_id: 'mid', title: 'C', bpm: 124.5 },
	{ stable_id: 'edge-high', title: 'D', bpm: 128 },
	{ stable_id: 'fast', title: 'E', bpm: 140 },
	{ stable_id: 'unknown', title: 'F', bpm: null }
];

const ids = (rows) => rows.map((r) => r.stable_id);

before(async () => {
	filters = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-column-filters.ts'
	);
	store = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-funnel-store.svelte.ts'
	);
	pane = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	explainers = await loadTypeScriptModule('src/lib/rb/planned-explainers.ts');
});

describe('track-table BPM funnel', () => {
	it('keeps only rows inside the inclusive range and lights the funnel', () => {
		const parsed = filters.parseBpmRangeInputs('120', '128');
		assert.equal(parsed.ok, true);
		assert.equal(filters.bpmFilterActive(parsed.filter), true);
		const all = { ...filters.EMPTY_TRACK_TABLE_COLUMN_FILTERS, bpm: parsed.filter };
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, all)), [
			'edge-low',
			'mid',
			'edge-high'
		]);
		const floorOnly = { ...all, bpm: filters.parseBpmRangeInputs('128', ' ').filter };
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(ROWS, floorOnly)), [
			'edge-high',
			'fast'
		]);
		assert.equal(filters.columnFiltersActiveCount(all), 1);
	});

	it('clears back to every row and keeps each pane its own range', () => {
		const s = new store.TrackTableFunnelStore();
		s.patchBpm('pane-a', { min: 120, max: 128 });
		assert.deepEqual(s.get('pane-b').bpm, filters.EMPTY_BPM_FILTER);
		assert.deepEqual(s.get('pane-a').bpm, { min: 120, max: 128 });
		s.patchBpm('pane-a', filters.EMPTY_BPM_FILTER);
		assert.equal(filters.bpmFilterActive(s.get('pane-a').bpm), false);
		assert.equal(filters.applyTrackTableColumnFilters(ROWS, s.get('pane-a')), ROWS);
		assert.equal(filters.parseBpmRangeInputs('', '').filter, filters.EMPTY_BPM_FILTER);
	});

	it('survives a sort change: the store is untouched and sorted rows narrow the same', () => {
		const s = new store.TrackTableFunnelStore();
		s.patchBpm('pane-a', { min: 120, max: 128 });
		s.patchColour('pane-a', filters.EMPTY_COLOUR_FILTER);
		const sorted = pane.sortRows(ROWS, 'bpm', -1);
		assert.deepEqual(s.get('pane-a').bpm, { min: 120, max: 128 });
		assert.deepEqual(ids(filters.applyTrackTableColumnFilters(sorted, s.get('pane-a'))), [
			'edge-high',
			'mid',
			'edge-low'
		]);
	});

	it('refuses min above max, negatives and junk with a reason', () => {
		for (const [min, max] of [
			['130', '120'],
			['-1', ''],
			['', 'fast']
		]) {
			const parsed = filters.parseBpmRangeInputs(min, max);
			assert.equal(parsed.ok, false, `${min}..${max}`);
			assert.ok(parsed.reason.length > 0);
		}
	});

	it('the planned column-filter stub no longer promises the BPM range', () => {
		const text = explainers.plannedTitle('track-table-filter');
		assert.doesNotMatch(text, /\(genre, key, BPM range, rating\)/);
		assert.match(text, /The BPM range.* own funnels? on the BPM/);
	});
});
