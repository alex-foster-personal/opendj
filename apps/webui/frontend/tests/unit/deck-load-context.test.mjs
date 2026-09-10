/**
 * The row itself: does a deck-load timing row actually come out carrying the
 * conditions it was measured under?
 *
 * deck-load-concurrency.test.mjs proves the interval math and
 * machine-pressure.test.mjs proves the stamp. Neither proves the ROW, and a
 * label that is computed correctly and then dropped on the way to
 * `recordPerfTiming` is worth nothing. So these cases drive `beginDeckLoad`
 * and `recordDeckLoad` and then read the real perf ring back through
 * `readPerfEvents()`, which is the same ring `__mdtPerfLog()` serves to
 * DevTools and to an agent.
 *
 * The solo pair is matched here too, for the reason spelled out in the
 * concurrency suite: a suite in which every row comes back solo cannot tell a
 * working instrument from a constant.
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://engine.example.test';

let context;
let ring;
let originalFetch;
let originalConsoleInfo;
let originalLocalStorage;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

/** A settled mix-only load, the plainest stem shape a row can carry. */
const MIX_ONLY = {
	status: 'unavailable',
	source: null,
	model: null,
	layout: null,
	error: 'no stem bundle'
};

function lastDeckLoadRow() {
	const rows = ring.readPerfEvents().filter((event) => event.kind.startsWith('deck-load'));
	assert.ok(rows.length > 0, 'expected a deck-load row in the ring');
	return rows[rows.length - 1];
}

/** Burn wall time, because these spans are real `performance.now()` intervals
 * rather than numbers a test hands in. */
function spin(ms) {
	const until = performance.now() + ms;
	while (performance.now() < until) {
		// Deliberately busy: a timer would let the module's own clock and the
		// test's disagree about what "still in flight" means.
	}
}

beforeEach(async () => {
	originalFetch = globalThis.fetch;
	originalConsoleInfo = console.info;
	originalLocalStorage = globalThis.localStorage;
	console.info = () => {};
	// No durable ring and no engine: this suite is about the row's SHAPE, and
	// both of those are other suites' subjects.
	defineGlobal('localStorage', undefined);
	defineGlobal('window', { addEventListener() {}, removeEventListener() {} });
	defineGlobal('fetch', () => Promise.reject(new Error('no engine under test')));
	// One bundle, so the context module and the ring it writes into are the
	// SAME module instances. Loading them separately would give the test a
	// different ring from the one the code under test appends to.
	context = await loadTypeScriptModule('tests/unit/fixtures/deck-load-context-probe.ts', {
		viteApiBase: API_BASE
	});
	ring = context;
});

afterEach(() => {
	defineGlobal('fetch', originalFetch);
	defineGlobal('localStorage', originalLocalStorage);
	console.info = originalConsoleInfo;
});

test('an uncontended load writes a row carrying solo=1', () => {
	const { clock: perfMs, spanId } = context.beginDeckLoad(1);
	spin(2);
	const stages = { total: perfMs() };
	context.recordDeckLoad('deck-load sid=aaaaaaaaaaaa', stages, 1, MIX_ONLY, spanId);

	const row = lastDeckLoadRow();
	assert.equal(row.labels.solo, '1');
	assert.equal(row.labels.concurrent_loads, '1');
	// The stem telemetry the row already carried is untouched by the merge.
	assert.equal(row.labels.stemLayout, 'none');
	assert.equal(row.stages.total, stages.total);
});

test('two overlapping loads BOTH write rows carrying solo=0', () => {
	// Deck 1 starts, deck 2 starts, deck 2 finishes first, then deck 1. The
	// row that lands FIRST is the one a reconstruct-at-write-time scheme would
	// wrongly call solo, so it is asserted explicitly rather than implied.
	const load1 = context.beginDeckLoad(1);
	spin(1);
	const load2 = context.beginDeckLoad(2);
	spin(1);
	context.recordDeckLoad('deck-load sid=bbbbbbbbbbbb', { total: load2.clock() }, 2, MIX_ONLY, load2.spanId);
	const firstRow = lastDeckLoadRow();
	assert.equal(firstRow.deck, 2);
	assert.equal(firstRow.labels.solo, '0', 'the load that finished first must still see its rival');
	assert.equal(firstRow.labels.concurrent_loads, '2');

	spin(1);
	context.recordDeckLoad('deck-load sid=cccccccccccc', { total: load1.clock() }, 1, MIX_ONLY, load1.spanId);
	const secondRow = lastDeckLoadRow();
	assert.equal(secondRow.deck, 1);
	assert.equal(secondRow.labels.solo, '0');
	assert.equal(secondRow.labels.concurrent_loads, '2');
});

test('a load that starts after another finished is solo again', () => {
	const loadA = context.beginDeckLoad(1);
	spin(1);
	context.recordDeckLoad('deck-load sid=dddddddddddd', { total: loadA.clock() }, 1, MIX_ONLY, loadA.spanId);
	assert.equal(lastDeckLoadRow().labels.solo, '1');

	spin(2);
	const loadB = context.beginDeckLoad(2);
	spin(1);
	context.recordDeckLoad('deck-load sid=eeeeeeeeeeee', { total: loadB.clock() }, 2, MIX_ONLY, loadB.spanId);
	const row = lastDeckLoadRow();
	assert.equal(row.labels.solo, '1');
	assert.equal(row.labels.concurrent_loads, '1');
});

test('with no pressure reading, the row says unknown and stamps no numbers', () => {
	const { clock: perfMs, spanId } = context.beginDeckLoad(1);
	spin(1);
	context.recordDeckLoad('deck-load sid=ffffffffffff', { total: perfMs() }, 1, MIX_ONLY, spanId);

	const row = lastDeckLoadRow();
	assert.equal(row.labels.pressure, 'unknown');
	for (const key of ['load_avg_1m', 'mem_free_mb', 'swap_used_mb', 'pressure_age_ms']) {
		assert.equal(row.labels[key], undefined, `${key} must be absent, not zero`);
	}
});

test('a cached reading reaches the row as numbers plus its age', async () => {
	defineGlobal('fetch', () =>
		Promise.resolve({
			ok: true,
			json: async () => ({
				available: true,
				load_avg_1m: 5.76,
				mem_free_mb: 67.7,
				swap_used_mb: 6535.4,
				cache_age_ms: 0
			})
		})
	);
	defineGlobal('document', {
		visibilityState: 'visible',
		addEventListener() {},
		removeEventListener() {}
	});
	const stop = context.startMachinePressurePolling();
	for (let i = 0; i < 5; i++) await Promise.resolve();

	const { clock: perfMs, spanId } = context.beginDeckLoad(3);
	spin(1);
	context.recordDeckLoad('deck-load sid=111111111111', { total: perfMs() }, 3, MIX_ONLY, spanId);
	stop();

	const row = lastDeckLoadRow();
	assert.equal(row.labels.load_avg_1m, '5.76');
	assert.equal(row.labels.mem_free_mb, '67.7');
	assert.equal(row.labels.swap_used_mb, '6535.4');
	assert.equal(row.labels.pressure, undefined, 'a real reading must not also say unknown');
	assert.ok(Number.isFinite(Number(row.labels.pressure_age_ms)));
});

test('a failed load records its conditions too', () => {
	const { clock: perfMs, spanId } = context.beginDeckLoad(4);
	spin(1);
	context.recordDeckLoad('deck-load-fail', { failedAt: perfMs() }, 4, MIX_ONLY, spanId);

	const row = lastDeckLoadRow();
	assert.equal(row.kind, 'deck-load-fail');
	assert.equal(row.labels.solo, '1');
	assert.equal(row.labels.pressure, 'unknown');
});

test('the console line carries the labels, because that is where people grep', () => {
	const lines = [];
	console.info = (line) => lines.push(line);
	const { clock: perfMs, spanId } = context.beginDeckLoad(1);
	spin(1);
	context.recordDeckLoad('deck-load sid=222222222222', { total: perfMs() }, 1, MIX_ONLY, spanId);
	assert.match(lines.at(-1), /solo=1/);
	assert.match(lines.at(-1), /concurrent_loads=1/);
	assert.match(lines.at(-1), /pressure=unknown/);
});

// --------------------------------------------- #1658 review: identity, not recency

test('two concurrent same-deck loads: the first to finish must not close the other span', () => {
	// X starts, then two independent other-deck witnesses (W1, W2) run and
	// finish -- briefly overlapping EACH OTHER -- entirely inside the window
	// before Y starts. Y then starts (also deck 1, concurrent with X). X
	// finishes FIRST while Y is still open.
	//
	// The old `_closeSpan` picked the newest OPEN span for the deck rather
	// than the span this call opened, so X's own finish would grab Y's span
	// instead of its own: X's row would end up bound to [Y.start, X.finish]
	// and MISS W1/W2 (which ended before Y started, so outside that window),
	// while Y's later row would end up bound to [X.start, Y.finish] and
	// WRONGLY see W1/W2 (which it never actually overlapped). A single
	// witness is not enough to catch this: the bug leaves X's own span
	// object still open in the registry, and that leftover coincidentally
	// stands in for one missing overlap. Two witnesses that overlap EACH
	// OTHER give a peak of 3 that the leftover (worth at most 1) cannot
	// reproduce, so the bug's miscount is visible in the numbers, not just
	// the boolean.
	const loadX = context.beginDeckLoad(1);
	spin(1);
	const w1 = context.beginDeckLoad(2);
	spin(1);
	const w2 = context.beginDeckLoad(3);
	spin(1);
	context.recordDeckLoad('deck-load sid=w1w1w1w1w1w1', { total: w1.clock() }, 2, MIX_ONLY, w1.spanId);
	spin(1);
	context.recordDeckLoad('deck-load sid=w2w2w2w2w2w2', { total: w2.clock() }, 3, MIX_ONLY, w2.spanId);
	spin(1);
	const loadY = context.beginDeckLoad(1);
	spin(1);
	context.recordDeckLoad('deck-load sid=xxxxxxxxxxxx', { total: loadX.clock() }, 1, MIX_ONLY, loadX.spanId);
	const xRow = lastDeckLoadRow();
	assert.equal(xRow.labels.solo, '0');
	assert.equal(
		xRow.labels.concurrent_loads,
		'3',
		'X must see the witnesses that overlapped it before Y started'
	);

	spin(1);
	context.recordDeckLoad('deck-load sid=yyyyyyyyyyyy', { total: loadY.clock() }, 1, MIX_ONLY, loadY.spanId);
	const yRow = lastDeckLoadRow();
	assert.equal(yRow.labels.solo, '0');
	assert.equal(
		yRow.labels.concurrent_loads,
		'2',
		'Y must not see witnesses that ended before it started'
	);
});

test('a slow load survives sixteen later loads on other decks, and still closes correctly', () => {
	// One slow load starts, then sixteen independent loads on another deck
	// start and finish while it is still running -- the exact count that
	// made the old unconditional tail slice (`_spans.slice(-MAX_TRACKED_SPANS)`
	// at push time) evict the slow span the instant the 16th filler pushed,
	// because that push was the 17th entry in the array and the slice always
	// dropped the OLDEST one regardless of whether it was still in flight.
	//
	// A witness that starts while the slow load is still open, after the
	// fillers, must see it: `solo=0`, `concurrent_loads=2`. Under the old
	// eviction the slow span was gone from the registry by then, so the
	// witness wrongly read `solo=1`.
	const slow = context.beginDeckLoad(1);
	for (let i = 0; i < 16; i++) {
		const filler = context.beginDeckLoad(2);
		spin(1);
		context.recordDeckLoad(`deck-load sid=filler${i}`, { total: filler.clock() }, 2, MIX_ONLY, filler.spanId);
	}
	const witness = context.beginDeckLoad(3);
	spin(1);
	context.recordDeckLoad('deck-load sid=witnesswitn', { total: witness.clock() }, 3, MIX_ONLY, witness.spanId);
	const witnessRow = lastDeckLoadRow();
	assert.equal(witnessRow.labels.solo, '0', 'the slow load must still be visible after 16 later loads');
	assert.equal(witnessRow.labels.concurrent_loads, '2');

	spin(1);
	context.recordDeckLoad('deck-load sid=slowslowslow', { total: slow.clock() }, 1, MIX_ONLY, slow.spanId);
	const slowRow = lastDeckLoadRow();
	assert.equal(slowRow.labels.solo, '0', 'the slow load must still close against its own real span, not a synthesized one');
});

test('closing the slow load first must not evict its own span before its witness can see it', () => {
	// Same fixture as above, close order reversed: the witness begins, THEN
	// the slow load closes, THEN the witness closes. At the instant the slow
	// load closes it has exactly 16 already-completed spans ahead of it (the
	// fillers) -- if the trim counts the slow span itself as the 17th
	// completed entry in that same pass, it splices itself out on the way to
	// closing, and the witness's own close later finds no trace of it in the
	// registry. That is Thread B's failure mode relocated from mid-flight
	// eviction to self-eviction at close time.
	const slow = context.beginDeckLoad(1);
	for (let i = 0; i < 16; i++) {
		const filler = context.beginDeckLoad(2);
		spin(1);
		context.recordDeckLoad(`deck-load sid=filler${i}`, { total: filler.clock() }, 2, MIX_ONLY, filler.spanId);
	}
	const witness = context.beginDeckLoad(3);
	spin(1);
	context.recordDeckLoad('deck-load sid=slowslowslow', { total: slow.clock() }, 1, MIX_ONLY, slow.spanId);

	spin(1);
	context.recordDeckLoad('deck-load sid=witnesswitn', { total: witness.clock() }, 3, MIX_ONLY, witness.spanId);
	const witnessRow = lastDeckLoadRow();
	assert.equal(witnessRow.labels.solo, '0', 'the witness genuinely overlapped the slow load, which had not closed when it began');
	assert.equal(witnessRow.labels.concurrent_loads, '2');
});
