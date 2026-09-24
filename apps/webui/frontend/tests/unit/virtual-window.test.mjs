import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { loadTypeScriptModule } from './load-typescript.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const TRACK_TABLE_PATH = join(
	__dirname,
	'../../src/lib/components/rb/browser/TrackTable.svelte'
);

// track-list-virtualization (browser-surface unit). Regression lines:
// - if computeVirtualWindow's start/end drift from scrollTop/rowHeight math then broken
// - if computeVirtualWindow treats scrollTop as pure row pixels when a sticky
//   thead is present then broken
// - if masterFoldVisibility reports on-screen for a row that is ~1 row past the
//   bottom fold then broken
// - if scrollTopForRowIndex omits the thead then jump-to-master and suggest-next
//   land ~1 row off
// - if overscan isn't clamped to [0, rowCount] then broken (negative index / out-of-range slice)
// - if topPad/bottomPad don't reconstruct the full scrollable height then the scrollbar lies
// - if fetchAllPages stops before next_cursor is null then rows silently truncate again
// - if fetchAllPages doesn't fail loudly past the safety ceiling then a pagination bug hides
// - if onPage receives out-of-order or non-cumulative counts then the ad59ac load
//   indicator would show progress jumping around instead of monotonically climbing
// - if onPage claims work finer than completed cursor pages then the pin is broken

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

test('computeVirtualWindow: a stale deep scrollTop from a since-shrunk list still yields a valid, non-empty window', () => {
	// Regression: e.g. the user scrolled to row 4990 of a 5000-row list,
	// then a search/filter narrowed rowCount to 10 without resetting
	// scrollTop. startIndex must not be left past endIndex (which would
	// slice to nothing behind a stale, oversized top spacer).
	const w = mod.computeVirtualWindow({
		scrollTop: 22 * 4990,
		viewportHeight: 220,
		rowHeight: 22,
		rowCount: 10,
		overscan: 10
	});
	assert.ok(w.startIndex <= w.endIndex);
	assert.equal(w.endIndex, 10);
	assert.equal(w.startIndex, 0);
	assert.equal(w.topPad, 0);
	assert.equal(w.bottomPad, 0);
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

test('TRACK_TABLE_THEAD_PX matches sticky thead CSS height', () => {
	assert.equal(mod.TRACK_TABLE_THEAD_PX, 20);
});

test('computeVirtualWindow: headerOffsetPx 0 matches omitting the param', () => {
	const base = {
		scrollTop: 22 * 1000,
		viewportHeight: 220,
		rowHeight: 22,
		rowCount: 5000,
		overscan: 10
	};
	const omitted = mod.computeVirtualWindow(base);
	const explicitZero = mod.computeVirtualWindow({ ...base, headerOffsetPx: 0 });
	assert.deepEqual(explicitZero, omitted);
});

test('computeVirtualWindow: sticky thead reduces visible row count by one', () => {
	const w = mod.computeVirtualWindow({
		scrollTop: 0,
		viewportHeight: 221,
		rowHeight: 22,
		rowCount: 5000,
		overscan: 0,
		headerOffsetPx: 20
	});
	assert.equal(w.endIndex, 10); // ceil(201/22) not ceil(221/22)
});

test('scrollTopForRowIndex: row 0 at offset 0 clamps to 0', () => {
	assert.equal(
		mod.scrollTopForRowIndex({ rowIndex: 0, rowHeight: 22, headerOffsetPx: 20, offsetFromTopPx: 0 }),
		0
	);
});

test('scrollTopForRowIndex: compact row 10 with thead and one-third viewport offset', () => {
	const offset = Math.floor(220 / 3); // 73
	const withHeader = mod.scrollTopForRowIndex({
		rowIndex: 10,
		rowHeight: 22,
		headerOffsetPx: 20,
		offsetFromTopPx: offset
	});
	assert.equal(withHeader, 20 + 10 * 22 - offset);
	const withoutHeader = mod.scrollTopForRowIndex({
		rowIndex: 10,
		rowHeight: 22,
		offsetFromTopPx: offset
	});
	assert.equal(withoutHeader, withHeader - 20);
});

test('scrollTopToKeepRowVisible: shrinking viewport keeps the anchored row visible', () => {
	const priorViewportHeight = 206;
	const priorScrollTop = 88;
	const rowIndex = 6;
	const rowHeight = 22;
	const header = mod.TRACK_TABLE_THEAD_PX;
	const next = mod.scrollTopToKeepRowVisible({
		rowIndex,
		rowHeight,
		headerOffsetPx: header,
		viewportHeight: 147,
		priorScrollTop,
		priorViewportHeight
	});
	const rowTop = header + rowIndex * rowHeight;
	const rowBottom = rowTop + rowHeight;
	assert.ok(next + 147 >= rowBottom - 1, 'row bottom stays in viewport after shrink');
	assert.ok(next + header <= rowTop + 1, 'row top stays in viewport after shrink');
});

test('masterFoldVisibility: absent index or zero viewport returns null', () => {
	assert.equal(
		mod.masterFoldVisibility({
			rowIndex: -1,
			rowHeight: 22,
			scrollTop: 0,
			viewportHeight: 220
		}),
		null
	);
	assert.equal(
		mod.masterFoldVisibility({
			rowIndex: 5,
			rowHeight: 22,
			scrollTop: 0,
			viewportHeight: 0
		}),
		null
	);
});

test('masterFoldVisibility: row fully above the band returns above', () => {
	assert.equal(
		mod.masterFoldVisibility({
			rowIndex: 10,
			rowHeight: 22,
			scrollTop: 500,
			viewportHeight: 220,
			headerOffsetPx: 20
		}),
		'above'
	);
});

test('masterFoldVisibility: row in the visible band returns null', () => {
	assert.equal(
		mod.masterFoldVisibility({
			rowIndex: 10,
			rowHeight: 22,
			scrollTop: 200,
			viewportHeight: 220,
			headerOffsetPx: 20
		}),
		null
	);
});

test('masterFoldVisibility: fold-below fires when row top reaches bottom edge', () => {
	const rowIndex = 50;
	const rowHeight = 22;
	const header = 20;
	const viewportHeight = 220;
	const rowTop = header + rowIndex * rowHeight; // 1120
	assert.equal(
		mod.masterFoldVisibility({
			rowIndex,
			rowHeight,
			scrollTop: rowTop - viewportHeight, // 900
			viewportHeight,
			headerOffsetPx: header
		}),
		'below'
	);
	// HEAD (no header in row top) still says on-screen here; row is back in band.
	assert.equal(
		mod.masterFoldVisibility({
			rowIndex,
			rowHeight,
			scrollTop: rowTop - viewportHeight + 20, // 920
			viewportHeight,
			headerOffsetPx: header
		}),
		null
	);
});

test('TrackTable.svelte wires thead compensation through virtual-window helpers', () => {
	const src = readFileSync(TRACK_TABLE_PATH, 'utf8');
	assert.match(src, /TRACK_TABLE_THEAD_PX/);
	assert.match(src, /masterFoldVisibility/);
	assert.match(src, /scrollTopForRowIndex/);
	assert.match(src, /from '\.\/virtual-window'/);
	assert.match(src, /headerOffsetPx:\s*TRACK_TABLE_THEAD_PX/);
	assert.match(src, /thead th[\s\S]*height:\s*20px/);
	assert.doesNotMatch(src, /AUTOPLAY_THEAD_H/);
	assert.match(src, /jumpToMaster[\s\S]*scrollTopForRowIndex/);
	assert.match(src, /masterFold[\s\S]*masterFoldVisibility/);
	const findScrollBlock = src.slice(src.indexOf('findQuery'), src.indexOf('masterIndex'));
	assert.match(findScrollBlock, /scrollTopForRowIndex/);
	const suggestBlock = src.slice(src.indexOf('suggestHoverId'), src.indexOf('jumpToMaster'));
	assert.match(suggestBlock, /scrollTopForRowIndex/);
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
	let calls = 0;
	await assert.rejects(
		() =>
			mod.fetchAllPages(async () => {
				calls += 1;
				return _page([1], `cursor-${calls}`);
			}, { maxPages: 3 }),
		/exceeded 3 pages/
	);
});

test('fetchAllPages: a repeated cursor fails before it can amplify requests and memory', async () => {
	let calls = 0;
	await assert.rejects(
		() =>
			mod.fetchAllPages(async () => {
				calls += 1;
				return _page([calls], 'stuck-cursor');
			}),
		/repeated cursor/
	);
	assert.equal(calls, 2);
});

test('fetchAllPages: a real library exactly N * pageSize long does not falsely trip the ceiling', async () => {
	// Regression: the backend hands back a non-null next_cursor whenever a
	// page comes back FULL (a 'maybe more' heuristic, not proof more rows
	// exist - see apps/webui/server/backend.py). A library whose size is an
	// exact multiple of the page size therefore needs one extra confirming
	// empty page before next_cursor goes null - the default ceiling must
	// tolerate that for any plausible real library, using the DEFAULT
	// maxPages (no override), at a page count well past the old 200-page
	// ceiling this regresses against.
	const PAGE_SIZE = 500;
	const FULL_PAGES = 250; // 125,000 rows - past the old 100k-row ceiling
	let pagesServed = 0;
	const rows = await mod.fetchAllPages(async () => {
		pagesServed += 1;
		if (pagesServed <= FULL_PAGES) {
			const items = new Array(PAGE_SIZE).fill(0).map((_, i) => pagesServed * 1000 + i);
			return _page(items, `cursor-${pagesServed}`); // full page - cursor non-null
		}
		return _page([], null); // confirming empty page - exhausted
	});
	assert.equal(rows.length, FULL_PAGES * PAGE_SIZE);
	assert.equal(pagesServed, FULL_PAGES + 1);
});

test('fetchAllPages: onPage receives cumulative counts in order, including the final confirming empty page', async () => {
	const pages = [_page([1, 2], 'c1'), _page([3, 4, 5], 'c2'), _page([], null)];
	const seen = [];
	const rows = await mod.fetchAllPages(
		async () => pages.shift(),
		{ onPage: (info) => seen.push({ ...info }) }
	);
	assert.deepEqual(rows, [1, 2, 3, 4, 5]);
	assert.deepEqual(seen, [
		{ loaded: 2, pageCount: 1 },
		{ loaded: 5, pageCount: 2 },
		{ loaded: 5, pageCount: 3 }
	]);
});

test('forEachCursorPage: walks in order and reports done on the last page', async () => {
	const pages = [_page([1, 2], 'c1'), _page([3], null)];
	const seen = [];
	const seenCursors = [];
	await mod.forEachCursorPage(
		async (cursor) => {
			seenCursors.push(cursor);
			return pages.shift();
		},
		{ onPage: (info) => seen.push({ items: info.items, loaded: info.loaded, pageCount: info.pageCount, done: info.done }) }
	);
	assert.deepEqual(seenCursors, [undefined, 'c1']);
	assert.deepEqual(seen, [
		{ items: [1, 2], loaded: 2, pageCount: 1, done: false },
		{ items: [3], loaded: 3, pageCount: 2, done: true }
	]);
});

test('forEachCursorPage: shouldContinue false after page 1 requests no further pages and does not throw', async () => {
	let calls = 0;
	let allow = true;
	await mod.forEachCursorPage(
		async () => {
			calls += 1;
			return _page([calls], `c${calls}`);
		},
		{
			shouldContinue: () => allow,
			onPage: (info) => {
				if (info.pageCount === 1) allow = false;
			}
		}
	);
	assert.equal(calls, 1);
});

test('forEachCursorPage: a repeated cursor still throws', async () => {
	let calls = 0;
	await assert.rejects(
		() =>
			mod.forEachCursorPage(
				async () => {
					calls += 1;
					return _page([calls], 'stuck-cursor');
				},
				{ onPage: () => {} }
			),
		/repeated cursor/
	);
	assert.equal(calls, 2);
});

test('forEachCursorPage: maxPages still throws instead of truncating', async () => {
	let calls = 0;
	await assert.rejects(
		() =>
			mod.forEachCursorPage(
				async () => {
					calls += 1;
					return _page([1], `cursor-${calls}`);
				},
				{ maxPages: 3, onPage: () => {} }
			),
		/exceeded/
	);
});

test('forEachCursorPage: exact N * pageSize still does the confirming empty page', async () => {
	const PAGE_SIZE = 4;
	const FULL_PAGES = 2;
	let pagesServed = 0;
	const seen = [];
	await mod.forEachCursorPage(
		async () => {
			pagesServed += 1;
			if (pagesServed <= FULL_PAGES) {
				return _page(new Array(PAGE_SIZE).fill(pagesServed), `c${pagesServed}`);
			}
			return _page([], null);
		},
		{ onPage: (info) => seen.push({ loaded: info.loaded, done: info.done, items: info.items.length }) }
	);
	assert.equal(pagesServed, FULL_PAGES + 1);
	assert.deepEqual(seen, [
		{ loaded: PAGE_SIZE, done: false, items: PAGE_SIZE },
		{ loaded: PAGE_SIZE * 2, done: false, items: PAGE_SIZE },
		{ loaded: PAGE_SIZE * 2, done: true, items: 0 }
	]);
});

test('if onPage claims work finer than completed cursor pages then the pin is broken', async () => {
	// onPage must fire exactly once per fetchPage resolution - never
	// interpolated between pages - so a caller driving a progress bar off it
	// can never show movement finer than a whole completed page.
	let fetchCalls = 0;
	let onPageCalls = 0;
	await mod.fetchAllPages(
		async () => {
			fetchCalls += 1;
			return fetchCalls <= 2 ? _page([fetchCalls], `c${fetchCalls}`) : _page([], null);
		},
		{ onPage: () => (onPageCalls += 1) }
	);
	assert.equal(onPageCalls, fetchCalls);
});
