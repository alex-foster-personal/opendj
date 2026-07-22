import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// track-list-virtualization (browser-surface unit). Regression lines:
// - if computeVirtualWindow's start/end drift from scrollTop/rowHeight math then broken
// - if overscan isn't clamped to [0, rowCount] then broken (negative index / out-of-range slice)
// - if topPad/bottomPad don't reconstruct the full scrollable height then the scrollbar lies
// - if fetchAllPages stops before next_cursor is null then rows silently truncate again
// - if fetchAllPages doesn't fail loudly past the safety ceiling then a pagination bug hides

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/virtual-window.ts');
});

// ------------------------------------------------------------ row window

test('computeVirtualWindow: empty pane renders nothing', () => {
	const w = mod.computeVirtualWindow({
		scrollTop: 0,
		viewportHeight: 400,
		rowHeight: 22,
		rowCount: 0,
		overscan: 10
	});
	assert.deepEqual(w, { startIndex: 0, endIndex: 0, topPad: 0, bottomPad: 0 });
});

test('computeVirtualWindow: not-yet-measured viewport (height 0) renders nothing', () => {
	const w = mod.computeVirtualWindow({
		scrollTop: 0,
		viewportHeight: 0,
		rowHeight: 22,
		rowCount: 5000,
		overscan: 10
	});
	assert.deepEqual(w, { startIndex: 0, endIndex: 0, topPad: 0, bottomPad: 0 });
});

test('computeVirtualWindow: top of a long list windows around scrollTop 0 with overscan clamped', () => {
	const w = mod.computeVirtualWindow({
		scrollTop: 0,
		viewportHeight: 220, // 10 rows visible
		rowHeight: 22,
		rowCount: 5000,
		overscan: 10
	});
	assert.equal(w.startIndex, 0); // overscan clamped, can't go negative
	assert.equal(w.endIndex, 20); // 10 visible + 10 overscan below
	assert.equal(w.topPad, 0);
	assert.equal(w.bottomPad, (5000 - 20) * 22);
});

test('computeVirtualWindow: middle of a long list windows both directions', () => {
	const w = mod.computeVirtualWindow({
		scrollTop: 22 * 1000, // scrolled to row 1000
		viewportHeight: 220,
		rowHeight: 22,
		rowCount: 5000,
		overscan: 10
	});
	assert.equal(w.startIndex, 990);
	assert.equal(w.endIndex, 1020);
	assert.equal(w.topPad, 990 * 22);
	assert.equal(w.bottomPad, (5000 - 1020) * 22);
});

test('computeVirtualWindow: bottom of a long list clamps endIndex to rowCount', () => {
	const w = mod.computeVirtualWindow({
		scrollTop: 22 * 4990,
		viewportHeight: 220,
		rowHeight: 22,
		rowCount: 5000,
		overscan: 10
	});
	assert.equal(w.endIndex, 5000);
	assert.equal(w.bottomPad, 0);
});

test('computeVirtualWindow: short list (fewer rows than viewport) windows the whole list', () => {
	const w = mod.computeVirtualWindow({
		scrollTop: 0,
		viewportHeight: 800,
		rowHeight: 22,
		rowCount: 5,
		overscan: 10
	});
	assert.equal(w.startIndex, 0);
	assert.equal(w.endIndex, 5);
	assert.equal(w.topPad, 0);
	assert.equal(w.bottomPad, 0);
});

test('computeVirtualWindow: negative scrollTop clamps to 0 instead of producing a negative index', () => {
	const w = mod.computeVirtualWindow({
		scrollTop: -50,
		viewportHeight: 220,
		rowHeight: 22,
		rowCount: 100,
		overscan: 5
	});
	assert.equal(w.startIndex, 0);
});

test('computeVirtualWindow: non-positive rowHeight throws (would divide toward NaN/Infinity indices)', () => {
	assert.throws(() =>
		mod.computeVirtualWindow({
			scrollTop: 0,
			viewportHeight: 220,
			rowHeight: 0,
			rowCount: 100,
			overscan: 5
		})
	);
});

// -------------------------------------------------------- cursor pagination

function _page(items, next_cursor) {
	return { items, next_cursor };
}

test('fetchAllPages: walks every page and concatenates in order', async () => {
	const pages = [_page([1, 2], 'c1'), _page([3, 4], 'c2'), _page([5], null)];
	const seenCursors = [];
	const rows = await mod.fetchAllPages(async (cursor) => {
		seenCursors.push(cursor);
		return pages.shift();
	});
	assert.deepEqual(rows, [1, 2, 3, 4, 5]);
	assert.deepEqual(seenCursors, [undefined, 'c1', 'c2']);
});

test('fetchAllPages: a single page (next_cursor null immediately) fetches once', async () => {
	let calls = 0;
	const rows = await mod.fetchAllPages(async () => {
		calls += 1;
		return _page(['a', 'b'], null);
	});
	assert.deepEqual(rows, ['a', 'b']);
	assert.equal(calls, 1);
});

test('fetchAllPages: an empty result set is a zero-length array, not an error', async () => {
	const rows = await mod.fetchAllPages(async () => _page([], null));
	assert.deepEqual(rows, []);
});

test('fetchAllPages: exceeding the safety ceiling fails loudly instead of truncating silently', async () => {
	await assert.rejects(
		() => mod.fetchAllPages(async () => _page([1], 'always-more'), { maxPages: 3 }),
		/exceeded 3 pages/
	);
});
