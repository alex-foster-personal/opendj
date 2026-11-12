// requirement: LIBUX-12871
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../../', import.meta.url));

let gap;

before(async () => {
	gap = await loadTypeScriptModule('src/lib/motion/row-insert-gap.ts');
});

const rows = ['a', 'b', 'c', 'd'].map((item_id) => ({ item_id }));

function gapAt(pointerY, draggedTrackCount = 1) {
	return gap.rowInsertGapFromPointer({
		pointerY,
		rowHeight: 20,
		slotCount: rows.length,
		headers: [],
		rows,
		draggedTrackCount
	});
}

describe('rowInsertGapTransform', () => {
	it('[if] a dragover frame sets height top or margin on a row [then] broken', () => {
		for (const g of [gapAt(45), gapAt(45, 3)]) {
			for (const slot of [g.slot - 1, g.slot, g.slot + 1]) {
				const css = gap.rowInsertGapTransform(slot, g);
				if (css === undefined) continue;
				assert.match(css, /^translateY\(\d+px\)$/);
				assert.doesNotMatch(css, /height|top|margin|padding/i);
			}
		}
	});
});

describe('rowInsertGapFromPointer', () => {
	it('[if] the gap index and the drop index disagree [then] broken', () => {
		const block = { item_ids: ['dragged'], section_ids: [] };
		const preview = gapAt(45);
		const move = { ...block, ...preview.target };
		assert.deepEqual(move, { ...block, before_item_id: 'c' });
		assert.equal(preview.slot, 2);
	});

	it('[if] prefers-reduced-motion is set and the gap still eases [then] broken', () => {
		assert.equal(gap.rowInsertGapDuration(1, true), 0);
		assert.equal(gap.rowInsertGapDuration(5, true), 0);
		assert.equal(gap.rowInsertGapDuration(1, false), gap.COLLAPSE_MS);
	});

	it('opens one row height per selected membership', () => {
		const multi = gapAt(45, 3);
		assert.equal(multi.count, 3);
		assert.equal(multi.offsetPx, 60);
		assert.equal(gap.rowInsertGapTransform(1, multi), undefined);
		assert.equal(gap.rowInsertGapTransform(2, multi), 'translateY(60px)');
	});
});

function trackTableSource() {
	return readFileSync(join(FRONTEND, 'src/lib/components/rb/browser/TrackTable.svelte'), 'utf8');
}

function sliceHandler(source, fromMarker, beforeMarker) {
	const start = source.indexOf(fromMarker);
	const end = source.indexOf(beforeMarker, start);
	assert.ok(start >= 0 && end > start, `${fromMarker} must exist`);
	return source.slice(start, end);
}

describe('TrackTable dragover stays off the virtual window', () => {
	it('[if] scrolling a virtualized playlist remounts every row on each dragover [then] broken', () => {
		const source = trackTableSource();
		const body = sliceHandler(source, 'function onBodyDragOver', 'function onBodyDragLeave');
		assert.doesNotMatch(body, /rowWindow|computeVirtualWindow|visibleRows|windowInfo/);
		assert.doesNotMatch(body, /\b(height|marginTop|top)\s*[:=]/);
	});
});

describe('TrackTable gap reverses when pointer leaves tbody', () => {
	it('[if] pointer leaves tbody and dropGap stays set [then] broken', () => {
		const body = sliceHandler(
			trackTableSource(),
			'function onBodyDragLeave',
			'function onBodyDrop'
		);
		assert.match(body, /relatedTarget/);
		assert.match(body, /getBoundingClientRect/);
		assert.match(body, /if \(!inside\) dropGap = null/);
		assert.doesNotMatch(body, /^\s*dropGap = null;/m);
	});
});

describe('TrackTable drop commits stored gap before clearing drag state', () => {
	it('[if] drop clears drag state before gap target is read [then] broken', () => {
		const source = trackTableSource();
		const body = sliceHandler(source, 'function onBodyDrop', 'async function toggleSection');
		const gapIdx = body.indexOf('const gap = dropGap');
		const endIdx = body.indexOf('_endEntriesDrag(');
		assert.ok(gapIdx >= 0 && endIdx > gapIdx, 'gap must be snapshotted before end drag');
		assert.match(body, /onmoveentries\?\.\(\{ \.\.\.block, \.\.\.gap\.target \}\)/);
		assert.match(body, /_endEntriesDrag\(\)/);
	});
});
