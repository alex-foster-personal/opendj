// requirement: REORDER-ROW-02, LIBUX-53
/**
 * REORDER-ROW-02
 * [if] a drop target differs from its preview [then] the reorder contract is broken [else stop].
 * LIBUX-53
 * [if] an N-track gap changes layout [then] table virtualization is broken [else stop].
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let support;

before(async () => {
	// Load the DOM-free gap helpers plus playlist-sections only; `track-table-support.ts`
	// re-exports them but bundles analysis-issues and pulls job-progress.svelte, which breaks
	// Node unit bundling for these assertions.
	support = await loadTypeScriptModule('src/lib/components/rb/browser/track-table-drop-gap.ts');
});

const rows = ['a', 'b', 'c', 'd'].map((item_id) => ({ item_id }));

function gapAt(pointerY, draggedTrackCount = 1) {
	return support.trackDropGapFromPointer({
		pointerY,
		rowHeight: 20,
		slotCount: rows.length,
		headers: [],
		rows,
		draggedTrackCount
	});
}

describe('trackDropGapFromPointer', () => {
	it('uses the preview target for upper- and lower-half drops', () => {
		const block = { item_ids: ['dragged'], section_ids: [] };
		const upperHalf = gapAt(45);
		const lowerHalf = gapAt(55);

		assert.deepEqual(upperHalf, {
			slot: 2,
			count: 1,
			offsetPx: 20,
			target: { before_item_id: 'c' }
		});
		assert.deepEqual(lowerHalf, {
			slot: 3,
			count: 1,
			offsetPx: 20,
			target: { before_item_id: 'd' }
		});

		const emittedUpperMove = { ...block, ...upperHalf.target };
		const emittedLowerMove = { ...block, ...lowerHalf.target };
		assert.deepEqual(emittedUpperMove, { ...block, before_item_id: 'c' });
		assert.deepEqual(emittedLowerMove, { ...block, before_item_id: 'd' });
	});

	it('opens one row height for every selected membership', () => {
		const block = { item_ids: ['drag-1', 'drag-2', 'drag-3'], section_ids: [] };
		const gap = gapAt(45, block.item_ids.length);

		assert.equal(gap.count, block.item_ids.length);
		assert.equal(gap.offsetPx, 60);
		assert.equal(support.trackDropGapTransform(1, gap), undefined);
		assert.equal(support.trackDropGapTransform(2, gap), 'translateY(60px)');
		assert.equal(support.trackDropGapTransform(3, gap), 'translateY(60px)');
		const emittedMove = { ...block, ...gap.target };
		assert.equal(emittedMove.item_ids.length, gap.count);
	});

	it('refuses an invalid dragged-track count', () => {
		for (const draggedTrackCount of [0, -1, 1.5, Number.NaN]) {
			assert.throws(() => gapAt(45, draggedTrackCount), RangeError);
		}
	});
});

describe('trackDropGapTransform', () => {
	it('returns translateY only for single- and multi-row gaps', () => {
		for (const gap of [gapAt(45), gapAt(45, 3)]) {
			for (const slot of [gap.slot - 1, gap.slot, gap.slot + 1]) {
				const offset = support.trackDropGapTransform(slot, gap);
				if (offset === undefined) continue;
				assert.match(offset, /^translateY\(\d+px\)$/);
				assert.doesNotMatch(offset, /height|top|margin|padding/i);
			}
		}
		assert.equal(support.trackDropGapTransform(2, gapAt(45)), 'translateY(20px)');
	});
});
