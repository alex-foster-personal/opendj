import assert from 'node:assert/strict';
import { after, before, mock, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// PERF-R5 Q9. Two failures this file exists to catch.
//
// 1. The local (Cmd+F) filter used to run on EVERY keystroke: BrowserPanel's
//    setSearch wrote pane.search straight through, and pane.search feeds a
//    $derived that runs filterRows + sortRows over the whole pane.rows array -
//    up to ~8k rows for All Tracks, plus an O(n log n) sort when a column sort
//    is active. Typing "deep house" was 10 full passes. The debounce below is
//    what collapses a burst into one.
// 2. There was NO timing on any library path at all: the cursor walk, the
//    filter recompute, the whole-collection FTS query and the row-select
//    prefetches were all unmeasured, so "the browser feels slow" had no number
//    behind it. These emitters are the perf ring rows that give it one.
//
// Regression lines:
// - if the debounce stops coalescing then a 10-char query is 10 full-array
//   filter+sort passes again
// - if it stops carrying the LAST query then the pane filters on a stale prefix
// - if a settled interaction stops emitting exactly one ring row then the ring
//   either loses the interaction or fills with per-keystroke noise
// - if the keystroke count leaves the ring row then a burst is indistinguishable
//   from a single keystroke when reading the log back
// - if cancel stops dropping a pending settle then a programmatic search write
//   (genre chip, clear) gets overwritten by the burst it superseded
// - if prefetch rows stop being sampled then a scroll-and-select sweep evicts
//   every other row from the 40-entry ring

let lib;

before(async () => {
	lib = await loadTypeScriptModule('tests/unit/fixtures/library-perf-entry.ts');
});

after(() => {
	mock.timers.reset();
});

/** Ring rows appended since `mark`, so tests compose in one shared ring. */
function rowsSince(mark) {
	return lib.readPerfEvents().slice(mark);
}

function ringMark() {
	return lib.readPerfEvents().length;
}

test('the local filter debounce is shorter than the whole-collection one', () => {
	// Local compute, not a network round trip: it only has to outlive a
	// keystroke burst. 250 ms (the FTS debounce) would be felt as lag.
	assert.ok(
		lib.LOCAL_FILTER_DEBOUNCE_MS >= 60 && lib.LOCAL_FILTER_DEBOUNCE_MS <= 100,
		`if the local debounce leaves 60-100 ms it is either still per-keystroke ` +
			`or visibly laggy (got ${lib.LOCAL_FILTER_DEBOUNCE_MS})`
	);
});

test('playlist tree readiness classifies a clean duration with no anomaly', () => {
	assert.deepEqual(
		lib.measurePlaylistTreeReadyMs(125),
		{ ms: 125, anomaly: null },
		'a real tree-ready measurement must preserve milliseconds and carry no anomaly'
	);
});

test('playlist tree readiness flags a clock-mismatch value and never throws (#5469)', () => {
	// #5469: "browser init failed: ... playlist tree ready duration must be
	// below 60 seconds, got 68271.9" was an unhandled rejection that aborted
	// BrowserPanel's whole boot. measurePlaylistTreeReadyMs must never throw -
	// a bad SAMPLE is not a reason to fail the measured app.
	assert.doesNotThrow(() => lib.measurePlaylistTreeReadyMs(0));
	assert.deepEqual(
		lib.measurePlaylistTreeReadyMs(0),
		{ ms: null, anomaly: 'epoch-mismatch' },
		'a zero duration hides a clock mismatch and is unusable, but must not throw'
	);
	assert.doesNotThrow(() => lib.measurePlaylistTreeReadyMs(lib.PLAYLIST_TREE_READY_EPOCH_MIN_MS));
	assert.deepEqual(
		lib.measurePlaylistTreeReadyMs(1_700_000_000_000),
		{ ms: null, anomaly: 'epoch-mismatch' },
		'an actual epoch timestamp (ms since 1970) must be rejected as unusable, not thrown'
	);
});

test('playlist tree readiness records a slow-but-real duration instead of throwing (#5469)', () => {
	// The real incident value: a genuinely slow tree-ready (background tab,
	// cold big library, loaded box) that is nowhere near epoch scale. It must
	// be classified 'slow' and KEPT, not discarded as if it were corrupt -
	// epoch-mismatch detection must distinguish the two, not conflate them.
	assert.doesNotThrow(() => lib.measurePlaylistTreeReadyMs(68_271.9));
	assert.deepEqual(
		lib.measurePlaylistTreeReadyMs(68_271.9),
		{ ms: 68272, anomaly: 'slow' },
		'a slow-but-real duration must still be recorded, flagged as slow, never thrown'
	);
	// Mutation control: a value just under the slow threshold must stay clean,
	// so the boundary is the MAX_MS constant and not an off-by-something.
	assert.deepEqual(
		lib.measurePlaylistTreeReadyMs(lib.PLAYLIST_TREE_READY_MAX_MS - 1),
		{ ms: lib.PLAYLIST_TREE_READY_MAX_MS - 1, anomaly: null },
		'just under the slow threshold must be clean, proving the slow flag is not firing unconditionally'
	);
});


test('a keystroke burst coalesces into ONE settle carrying the last query', () => {
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
	try {
		const settled = [];
		const debounce = lib.createFilterDebounce((s) => settled.push(s), 80);
		for (const q of ['d', 'de', 'dee', 'deep', 'deep ', 'deep h']) {
			debounce.push(q);
			mock.timers.tick(10);
		}
		assert.equal(settled.length, 0, 'nothing may settle while the burst is still arriving');
		mock.timers.tick(80);
		assert.equal(
			settled.length,
			1,
			'if 6 keystrokes produce more than one settle then the full-array ' +
				'filter+sort runs more than once for one typed word'
		);
		assert.equal(settled[0].query, 'deep h', 'the settle must carry the LAST query, not a prefix');
		assert.equal(
			settled[0].keystrokes,
			6,
			'if the coalesced count is lost then the ring cannot show a burst as a burst'
		);
		assert.ok(
			settled[0].coalescedMs >= 80 + 50,
			'coalescedMs must span the FIRST keystroke to the settle (50 ms of typing ' +
				`plus the 80 ms wait), got ${settled[0].coalescedMs}`
		);
	} finally {
		mock.timers.reset();
	}
});

test('a keystroke after the window starts a fresh interaction', () => {
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
	try {
		const settled = [];
		const debounce = lib.createFilterDebounce((s) => settled.push(s), 80);
		debounce.push('a');
		mock.timers.tick(80);
		debounce.push('ab');
		mock.timers.tick(80);
		assert.equal(settled.length, 2, 'two separated keystrokes are two interactions');
		assert.deepEqual(
			settled.map((s) => s.keystrokes),
			[1, 1],
			'if the counter is not reset then every later interaction over-reports its burst'
		);
	} finally {
		mock.timers.reset();
	}
});

test('cancel drops a pending settle and flush runs it immediately', () => {
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
	try {
		const settled = [];
		const debounce = lib.createFilterDebounce((s) => settled.push(s), 80);
		debounce.push('house');
		assert.equal(debounce.pending, true);
		debounce.cancel();
		mock.timers.tick(500);
		assert.equal(
			settled.length,
			0,
			'if cancel does not drop the pending settle then a genre chip click is ' +
				'overwritten 80 ms later by the burst it replaced'
		);

		debounce.push('techno');
		debounce.flush();
		assert.equal(settled.length, 1, 'flush must settle without waiting out the delay');
		assert.equal(settled[0].query, 'techno');
		assert.equal(debounce.pending, false);
	} finally {
		mock.timers.reset();
	}
});

test('one settled filter interaction emits exactly one library-filter ring row', () => {
	const mark = ringMark();
	lib.recordFilterTiming({
		keystrokes: 6,
		coalescedMs: 130,
		computeMs: 41.6,
		rowsIn: 8355,
		rowsOut: 27
	});
	const rows = rowsSince(mark);
	assert.equal(
		rows.length,
		1,
		'if one settled interaction emits more than one row then the ring is back ' +
			'to per-keystroke noise'
	);
	assert.equal(rows[0].kind, 'library-filter', 'the ring row name is the perf queue contract');
	assert.deepEqual(rows[0].stages, {
		keystrokes: 6,
		coalesced_ms: 130,
		compute_ms: 42,
		rows_in: 8355,
		rows_out: 27
	});
});

test('library load rows name the source and carry the walked row count', () => {
	const mark = ringMark();
	lib.recordLibraryLoadTiming('all-tracks', { fetchMs: 2410.4, rows: 8355 });
	lib.recordLibraryLoadTiming('playlist', { fetchMs: 118.9, rows: 35 });
	const rows = rowsSince(mark);
	assert.deepEqual(
		rows.map((r) => r.kind),
		['library-load-all-tracks', 'library-load-playlist'],
		'if both loads share one kind then the 8k cursor walk cannot be told apart ' +
			'from a 35-track playlist hydrate when reading the ring back'
	);
	assert.deepEqual(rows[0].stages, { fetch_ms: 2410, rows: 8355 });
	assert.deepEqual(rows[1].stages, { fetch_ms: 119, rows: 35 });
});

test('a completed whole-collection search emits one library-search-collection row', () => {
	const mark = ringMark();
	lib.recordCollectionSearchTiming({ queryMs: 96.2, hits: 200, total: 481 });
	const rows = rowsSince(mark);
	assert.equal(rows.length, 1);
	assert.equal(rows[0].kind, 'library-search-collection');
	assert.deepEqual(
		rows[0].stages,
		{ query_ms: 96, hits: 200, total: 481 },
		'total must ride alongside hits: a capped result set that reports only ' +
			'hits hides how much the FTS query actually matched'
	);
});

test('row-select prefetch rows are sampled, not emitted per select', () => {
	assert.equal(lib.PREFETCH_SAMPLE_EVERY, 5, 'the documented sample rate is 1 in 5');
	const mark = ringMark();
	const emitted = [];
	for (let i = 0; i < 10; i++) {
		emitted.push(lib.recordAnlzPrefetchSampled(12 + i, 'ready'));
	}
	assert.equal(
		emitted.filter(Boolean).length,
		2,
		'if every prefetch emits then a 10-row select sweep evicts a quarter of the ' +
			'40-entry ring; 1 in 5 keeps it a sample'
	);
	assert.equal(emitted[0], true, 'the FIRST prefetch of a session must always be timed');
	const rows = rowsSince(mark);
	assert.equal(rows.length, 2);
	assert.equal(rows[0].kind, 'library-prefetch-anlz');
	assert.equal(rows[0].stages.sample_of, 5, 'the row must state the rate it was sampled at');
	assert.equal(rows[0].stages.ok, 1);
});

test('sampled audio prefetch rows carry the fetched size, not just the time', () => {
	const mark = ringMark();
	const emitted = lib.recordAudioPrefetchSampled(1052.3, 8 * 1024 * 1024);
	assert.equal(emitted, true, 'the first audio prefetch of a session is always timed');
	const rows = rowsSince(mark);
	assert.equal(rows.length, 1);
	assert.equal(rows[0].kind, 'library-prefetch-audio');
	assert.deepEqual(
		rows[0].stages,
		{ fetch_ms: 1052, mib: 8, sample_of: 5 },
		'without the size, a slow prefetch cannot be told apart from a large one'
	);
});

test('recordPlaylistTreeReadyMs skips an unusable sample and records a slow one (#5469)', () => {
	// Placed last in this file on purpose: the perf ring's 'other' bucket is
	// shared across every library-* kind at a fixed budget (8), and this test
	// alone adds 3 'other' rows. rowsSince()/ringMark() diff by a snapshotted
	// array INDEX, which an eviction elsewhere in the bucket can invalidate by
	// shifting everything after it - exactly what broke the row-select
	// prefetch tests the first time this test ran before them in the file.
	// lastOfKind here scans from the newest entry backwards instead, which
	// stays correct no matter what gets evicted earlier in the bucket: the
	// row this test just pushed is always the newest of its kind.
	function lastOfKind(kind) {
		const events = lib.readPerfEvents();
		for (let i = events.length - 1; i >= 0; i--) {
			if (events[i].kind === kind) return events[i];
		}
		return undefined;
	}
	function countOfKind(kind) {
		return lib.readPerfEvents().filter((e) => e.kind === kind).length;
	}

	const timingCountBeforeEpoch = countOfKind('library-playlist-tree-ready');
	lib.recordPlaylistTreeReadyMs(lib.measurePlaylistTreeReadyMs(1_700_000_000_000));
	const epochAnomaly = lastOfKind('library-playlist-tree-ready-anomaly');
	assert.ok(epochAnomaly, 'an epoch-mismatch sample must still be logged as a named perf-event warning');
	assert.match(epochAnomaly.message, /epoch-mismatch/);
	assert.equal(
		countOfKind('library-playlist-tree-ready'),
		timingCountBeforeEpoch,
		'an epoch-mismatch sample must never be recorded as a timing'
	);

	const timingCountBeforeSlow = countOfKind('library-playlist-tree-ready');
	lib.recordPlaylistTreeReadyMs(lib.measurePlaylistTreeReadyMs(68_271.9));
	const slowAnomaly = lastOfKind('library-playlist-tree-ready-anomaly');
	assert.ok(slowAnomaly, 'a slow-but-real sample must also be flagged so it is findable without scanning every row');
	assert.match(slowAnomaly.message, /slow/);
	const timingRow = lastOfKind('library-playlist-tree-ready');
	assert.ok(timingRow, 'a slow-but-real sample must still land in the timing ring');
	assert.equal(timingRow.stages?.ready_ms, 68272);
	assert.equal(
		countOfKind('library-playlist-tree-ready'),
		timingCountBeforeSlow + 1,
		'exactly one timing row must be added for the slow sample, none for the epoch one'
	);
});
