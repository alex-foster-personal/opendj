import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// All Tracks progressive fill (issue #318). Regression lines:
// - if page 1 does not clear loading before page 2 is fetched, then the
//   pane stays on loading... for the whole walk
// - if a newer beginLoad still receives appends from the old walker, then
//   stale pages leak into the new pane
// - if a repeated cursor after first paint hides the table or silent-
//   truncates, then broken
// - if a page-1 pagination failure still paints, then failLoad cannot own
//   the empty pane
// - if an exact N * pageSize confirming empty page marks truncated, then
//   a complete library looks capped

let contract;
let fill;

before(async () => {
	contract = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-contract.svelte.ts'
	);
	fill = await loadTypeScriptModule(
		'src/lib/components/rb/browser/fill-all-tracks.ts'
	);
});

function _page(items, next_cursor) {
	return { items, next_cursor };
}

function mapRow(item, order) {
	return { stable_id: String(item), order };
}

async function fillPane(pane, seq, fetchPage, hooks = {}) {
	return fill.fillAllTracksPane({
		pane,
		seq,
		fetchPage,
		mapRow,
		progressTotal: 9,
		...hooks
	});
}

test('after page 1 of a 3-page walk, loading is false before page 2 is invoked', async () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	let fetchCalls = 0;
	let releasePage2;
	const page2Gate = new Promise((resolve) => {
		releasePage2 = resolve;
	});
	let firstPaintCalls = 0;
	let painted;
	const paintedP = new Promise((resolve) => {
		painted = resolve;
	});

	const walk = fillPane(p, seq, async (cursor) => {
		fetchCalls += 1;
		if (fetchCalls === 1) {
			assert.equal(cursor, undefined);
			return _page(['a', 'b'], 'c1');
		}
		if (fetchCalls === 2) {
			await page2Gate;
			return _page(['c', 'd'], 'c2');
		}
		return _page(['e'], null);
	}, {
		onFirstPaint: () => {
			firstPaintCalls += 1;
			assert.equal(fetchCalls, 1, 'page 2 must not be invoked before first paint');
			assert.equal(p.loading, false);
			assert.equal(p.rows.length, 2);
			assert.deepEqual(p.rows.map((r) => r.stable_id), ['a', 'b']);
			painted();
		}
	});

	await paintedP;
	assert.equal(firstPaintCalls, 1);
	assert.equal(p.loading, false);
	assert.equal(p.rows.length, 2);
	assert.notEqual(p.load_progress, null);

	releasePage2();
	await walk;

	assert.deepEqual(p.rows.map((r) => r.stable_id), ['a', 'b', 'c', 'd', 'e']);
	assert.deepEqual(p.rows.map((r) => r.order), [1, 2, 3, 4, 5]);
	assert.equal(p.load_progress, null);
	assert.equal(p.truncated, false);
	assert.equal(p.loading, false);
	assert.equal(fetchCalls, 3);
});

test('beginLoad with a new seq after page 1 must not append page 2 and must not fetch page 3', async () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	let fetchCalls = 0;
	let releasePage2;
	const page2Gate = new Promise((resolve) => {
		releasePage2 = resolve;
	});
	let painted;
	const paintedP = new Promise((resolve) => {
		painted = resolve;
	});

	const walk = fillPane(p, seq, async () => {
		fetchCalls += 1;
		if (fetchCalls === 1) return _page(['a'], 'c1');
		if (fetchCalls === 2) {
			await page2Gate;
			return _page(['b'], 'c2');
		}
		return _page(['c'], null);
	}, { onFirstPaint: painted });

	await paintedP;
	assert.equal(p.rows.length, 1);

	const fresh = p.beginLoad('pl-1', 'Warmup');
	releasePage2();
	await walk;

	assert.equal(p.isCurrentLoad(fresh), true);
	assert.deepEqual(p.rows, []);
	assert.equal(p.loading, true);
	assert.ok(fetchCalls <= 2, `stale walker must not request page 3, got ${fetchCalls} fetches`);
});

test('re-click All Tracks: old walker stops and the new seq owns the pane', async () => {
	const p = contract.createPaneStore();
	const seq1 = p.beginLoad('all', 'All Tracks');
	let fetchCalls = 0;
	let releasePage2;
	const page2Gate = new Promise((resolve) => {
		releasePage2 = resolve;
	});
	let painted;
	const paintedP = new Promise((resolve) => {
		painted = resolve;
	});

	const walk1 = fillPane(p, seq1, async () => {
		fetchCalls += 1;
		if (fetchCalls === 1) return _page(['old-a'], 'c1');
		if (fetchCalls === 2) {
			await page2Gate;
			return _page(['old-b'], null);
		}
		throw new Error('stale walker requested a third page');
	}, { onFirstPaint: painted });

	await paintedP;
	const seq2 = p.beginLoad('all', 'All Tracks');
	assert.equal(p.loading, true);
	assert.deepEqual(p.rows, []);

	const walk2 = fillPane(p, seq2, async () => _page(['new-a', 'new-b'], null));
	releasePage2();
	await Promise.all([walk1, walk2]);

	assert.equal(p.isCurrentLoad(seq2), true);
	assert.deepEqual(p.rows.map((r) => r.stable_id), ['new-a', 'new-b']);
	assert.equal(p.loading, false);
	assert.equal(p.error, null);
});

test('repeated cursor after first paint toasts via onFillError, keeps rows, does not set error', async () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	const fillErrors = [];
	let completes = 0;

	await fillPane(
		p,
		seq,
		async () => _page(['a'], 'stuck-cursor'),
		{
			onFillError: (error) => fillErrors.push(error),
			onComplete: () => {
				completes += 1;
			}
		}
	);

	assert.equal(fillErrors.length, 1);
	assert.match(fillErrors[0], /repeated cursor/);
	assert.equal(p.truncated, true);
	assert.ok(p.rows.length >= 1);
	assert.equal(p.rows[0].stable_id, 'a');
	assert.equal(p.error, null);
	assert.equal(p.loading, false);
	assert.equal(p.load_progress, null);
	assert.equal(completes, 0);
});

test('repeated cursor on page 1 throws and never publishes', async () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	let fillErrors = 0;

	await assert.rejects(
		() =>
			fillPane(
				p,
				seq,
				async () => {
					throw new Error('fetchAllPages: repeated cursor "x" - likely a pagination bug');
				},
				{
					onFillError: () => {
						fillErrors += 1;
					}
				}
			),
		/repeated cursor/
	);

	assert.equal(fillErrors, 0);
	assert.deepEqual(p.rows, []);
	assert.equal(p.loading, true);
	assert.equal(p.error, null);
	assert.equal(p.failLoad(seq, 'fetchAllPages: repeated cursor "x" - likely a pagination bug'), true);
	assert.match(p.error, /repeated cursor/);
	assert.equal(p.loading, false);
});

test('single-page library fetches once, loading false, no hanging progress', async () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	let fetchCalls = 0;
	let firstPaint = 0;
	let completed = null;

	await fillPane(p, seq, async () => {
		fetchCalls += 1;
		return _page(['only'], null);
	}, {
		onFirstPaint: () => {
			firstPaint += 1;
		},
		onComplete: (info) => {
			completed = info;
		}
	});

	assert.equal(fetchCalls, 1);
	assert.equal(firstPaint, 1);
	assert.equal(p.loading, false);
	assert.equal(p.load_progress, null);
	assert.equal(p.truncated, false);
	assert.deepEqual(p.rows.map((r) => r.stable_id), ['only']);
	assert.equal(completed.rows, 1);
	assert.ok(completed.fetchMs >= 0);
});

test('N * pageSize confirming empty page does not mark truncated', async () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('all', 'All Tracks');
	const PAGE_SIZE = 2;
	let pagesServed = 0;

	await fillPane(p, seq, async () => {
		pagesServed += 1;
		if (pagesServed === 1) return _page(['a', 'b'], 'c1');
		return _page([], null);
	});

	assert.equal(pagesServed, 2);
	assert.equal(p.rows.length, PAGE_SIZE);
	assert.deepEqual(p.rows.map((r) => r.order), [1, 2]);
	assert.equal(p.truncated, false);
	assert.equal(p.load_progress, null);
	assert.equal(p.loading, false);
});

test('cancelled walk before first page does not record onComplete', async () => {
	const p = contract.createPaneStore();
	p.beginLoad('all', 'All Tracks');
	const seq = p.beginLoad('pl-1', 'Warmup');
	let fetchCalls = 0;
	let completes = 0;

	await fillPane(
		p,
		seq - 1,
		async () => {
			fetchCalls += 1;
			return _page(['a'], null);
		},
		{
			onComplete: () => {
				completes += 1;
			}
		}
	);

	assert.equal(fetchCalls, 0);
	assert.equal(completes, 0);
	assert.equal(p.loading, true);
	assert.deepEqual(p.rows, []);
});
