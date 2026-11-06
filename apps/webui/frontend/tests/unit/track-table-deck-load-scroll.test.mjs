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

// issue #2606 / LIBUX-2606 browse-scroll module invariants and TrackTable wiring.
// End-to-end scroll preservation lives in TrackTable.stories.ts KeepsScrollWindowAcrossDeckLoad.

const rowHeight = 22;
const overscan = 10;
const headerOffsetPx = 20;
const viewportTall = 220;
const viewportShort = 160;
const rowCount = 80;
const N = 40;
const M = 5;

let vw;
let bs;

function makeRows(count) {
	return Array.from({ length: count }, (_, i) => ({ order: i }));
}

function windowAt(scrollTop, viewportHeight, count = rowCount) {
	return vw.computeVirtualWindow({
		scrollTop,
		viewportHeight,
		rowHeight,
		rowCount: count,
		overscan,
		headerOffsetPx
	});
}

function rowContains(w, index) {
	return w.startIndex <= index && index < w.endIndex;
}

before(async () => {
	vw = await loadTypeScriptModule('src/lib/components/rb/browser/virtual-window.ts');
	bs = await loadTypeScriptModule('src/lib/components/rb/browser/browse-scroll.ts');
});

test('stale browseScroll revision must not re-apply; replay at short viewport would unmount N', () => {
	const rows = makeRows(rowCount);
	const slotOfRow = (index) => index;
	const staleRequest = { order: M, direction: 1, revision: 1 };
	assert.equal(bs.shouldApplyBrowseScroll(staleRequest, 1), false);

	const operatorAtN = vw.scrollTopForRowIndex({
		rowIndex: N,
		rowHeight,
		headerOffsetPx,
		offsetFromTopPx: viewportShort * 0.35
	});
	const wKept = windowAt(operatorAtN, viewportShort);
	assert.ok(rowContains(wKept, N), 'virtual window at operator scroll still contains N after shrink');

	const replayTarget = bs.browseScrollTarget({
		request: staleRequest,
		rows,
		viewportHeight: viewportShort,
		rowHeight,
		headerOffsetPx,
		slotOfRow
	});
	assert.ok(replayTarget !== null);
	const wIfReplay = windowAt(replayTarget, viewportShort);
	assert.ok(!rowContains(wIfReplay, N), 'applying stale browseScroll scrollTop would unmount N');
});

test('exact reveal of off-window row via new browseScroll revision', () => {
	const rows = makeRows(rowCount);
	const slotOfRow = (index) => index;
	const atM = bs.browseScrollTarget({
		request: { order: M, direction: 1, revision: 1 },
		rows,
		viewportHeight: viewportTall,
		rowHeight,
		headerOffsetPx,
		slotOfRow
	});
	const wAtM = windowAt(atM, viewportTall);
	assert.ok(rowContains(wAtM, M));
	assert.ok(!rowContains(wAtM, N));

	const reveal = { order: N, direction: 1, revision: 2 };
	assert.equal(bs.shouldApplyBrowseScroll(reveal, 1), true);
	const target = bs.browseScrollTarget({
		request: reveal,
		rows,
		viewportHeight: viewportTall,
		rowHeight,
		headerOffsetPx,
		slotOfRow
	});
	assert.ok(Number.isFinite(target));
	const wReveal = windowAt(target, viewportTall);
	assert.ok(rowContains(wReveal, N));
});

test('nextMeasuredViewportHeight ignores 0-height blip after positive measure', () => {
	assert.equal(bs.nextMeasuredViewportHeight(0, 220), 220);
	assert.equal(bs.nextMeasuredViewportHeight(160, 220), 160);
	assert.equal(bs.nextMeasuredViewportHeight(0, 0), 0);

	const scrollTopN = vw.scrollTopForRowIndex({
		rowIndex: N,
		rowHeight,
		headerOffsetPx,
		offsetFromTopPx: viewportTall * 0.35
	});
	const effectiveHeight = bs.nextMeasuredViewportHeight(0, viewportTall);
	const w = windowAt(scrollTopN, effectiveHeight);
	assert.ok(rowContains(w, N));
});

test('shouldRestorePaneScroll only on restoreKey change', () => {
	assert.equal(bs.shouldRestorePaneScroll('0:1', null), true);
	assert.equal(bs.shouldRestorePaneScroll('0:1', '0:1'), false);
	assert.equal(bs.shouldRestorePaneScroll('0:2', '0:1'), true);
});

test('TrackTable wiring uses browse-scroll helpers (source)', () => {
	const src = readFileSync(TRACK_TABLE_PATH, 'utf8');
	assert.match(src, /from '\.\/browse-scroll'/);
	assert.match(src, /shouldApplyBrowseScroll/);
	assert.match(src, /browseScrollTarget/);
	assert.match(src, /shouldRestorePaneScroll/);
	assert.match(src, /nextMeasuredViewportHeight/);
	assert.match(src, /untrack\(\(\)\s*=>\s*\n?\s*browseScrollTarget/);
	assert.match(src, /shouldRestorePaneScroll\(restoreKey/);
	const effectBlocks = src.match(/\$effect\(\(\) => \{[\s\S]*?\n\t\}\);/g) ?? [];
	for (const block of effectBlocks) {
		if (/loadedIds/.test(block) && /(\.scrollTop\s*=|liveScrollTop\s*=)/.test(block)) {
			assert.fail('TrackTable must not assign scroll from loadedIds in an $effect');
		}
		if (/masterStableId/.test(block) && /(\.scrollTop\s*=|liveScrollTop\s*=)/.test(block)) {
			assert.fail('TrackTable must not assign scroll from masterStableId in an $effect');
		}
	}
});
