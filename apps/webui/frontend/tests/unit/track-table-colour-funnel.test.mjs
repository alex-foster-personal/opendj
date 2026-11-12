import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let filters;

describe('track-table colour funnel', () => {
	before(async () => {
		filters = await loadTypeScriptModule(
			'src/lib/components/rb/browser/track-table-column-filters.ts'
		);
	});

	it('lists eight swatches plus no colour, OR-multi-select, active state, and clear', async () => {
		/** [if] several colours are picked [then] rows match any and clear restores all, [else stop]. */
		const ids = [...filters.REKORDBOX_TRACK_COLOR_IDS, filters.TRACK_COLOUR_NONE];
		assert.equal(ids.length, 9);

		let colour = filters.EMPTY_COLOUR_FILTER;
		assert.equal(filters.colourFilterActive(colour), false);

		colour = filters.toggleColourSelection(colour, 1);
		colour = filters.toggleColourSelection(colour, 3);
		assert.equal(filters.colourFilterActive(colour), true);
		assert.deepEqual(colour.selected, [1, 3]);

		const rows = [
			{ stable_id: 'a', color_id: 1 },
			{ stable_id: 'b', color_id: 3 },
			{ stable_id: 'c', color_id: 2 },
			{ stable_id: 'd', color_id: null }
		];
		const narrowed = filters.applyTrackTableColumnFilters(rows, {
			...filters.EMPTY_TRACK_TABLE_COLUMN_FILTERS,
			colour
		});
		assert.deepEqual(
			narrowed.map((r) => r.stable_id),
			['a', 'b']
		);

		colour = filters.clearColourFilter();
		assert.equal(filters.colourFilterActive(colour), false);
		assert.equal(
			filters.applyTrackTableColumnFilters(rows, {
				...filters.EMPTY_TRACK_TABLE_COLUMN_FILTERS,
				colour
			}),
			rows
		);
	});

	it('AND-composes with other column funnels and ignores sort order', () => {
		/** [if] colour is active with BPM and mytag funnels [then] only rows match all columns, [else stop]. */
		const colour = { selected: [1] };
		const tableFilters = {
			...filters.EMPTY_TRACK_TABLE_COLUMN_FILTERS,
			colour,
			bpm: { min: 120, max: 130 },
			mytag: { tag: 'house' }
		};
		const forward = [
			{ stable_id: 'hit', color_id: 1, bpm: 124, tags: ['house'] },
			{ stable_id: 'wrong-colour', color_id: 3, bpm: 124, tags: ['house'] },
			{ stable_id: 'wrong-bpm', color_id: 1, bpm: 100, tags: ['house'] },
			{ stable_id: 'wrong-tag', color_id: 1, bpm: 124, tags: ['techno'] }
		];
		const reversed = [...forward].reverse();
		for (const rows of [forward, reversed]) {
			const out = filters.applyTrackTableColumnFilters(rows, tableFilters);
			assert.deepEqual(
				out.map((r) => r.stable_id),
				['hit']
			);
		}
	});
});
