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
	const perfMs = context.beginDeckLoad(1);
	spin(2);
	const stages = { total: perfMs() };
	context.recordDeckLoad('deck-load sid=aaaaaaaaaaaa', stages, 1, MIX_ONLY);

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
	const perfMs1 = context.beginDeckLoad(1);
	spin(1);
	const perfMs2 = context.beginDeckLoad(2);
	spin(1);
	context.recordDeckLoad('deck-load sid=bbbbbbbbbbbb', { total: perfMs2() }, 2, MIX_ONLY);
	const firstRow = lastDeckLoadRow();
	assert.equal(firstRow.deck, 2);
	assert.equal(firstRow.labels.solo, '0', 'the load that finished first must still see its rival');
	assert.equal(firstRow.labels.concurrent_loads, '2');

	spin(1);
	context.recordDeckLoad('deck-load sid=cccccccccccc', { total: perfMs1() }, 1, MIX_ONLY);
	const secondRow = lastDeckLoadRow();
	assert.equal(secondRow.deck, 1);
	assert.equal(secondRow.labels.solo, '0');
	assert.equal(secondRow.labels.concurrent_loads, '2');
});

test('a load that starts after another finished is solo again', () => {
	const perfMsA = context.beginDeckLoad(1);
	spin(1);
	context.recordDeckLoad('deck-load sid=dddddddddddd', { total: perfMsA() }, 1, MIX_ONLY);
	assert.equal(lastDeckLoadRow().labels.solo, '1');

	spin(2);
	const perfMsB = context.beginDeckLoad(2);
	spin(1);
	context.recordDeckLoad('deck-load sid=eeeeeeeeeeee', { total: perfMsB() }, 2, MIX_ONLY);
	const row = lastDeckLoadRow();
	assert.equal(row.labels.solo, '1');
	assert.equal(row.labels.concurrent_loads, '1');
});

test('with no pressure reading, the row says unknown and stamps no numbers', () => {
	const perfMs = context.beginDeckLoad(1);
	spin(1);
	context.recordDeckLoad('deck-load sid=ffffffffffff', { total: perfMs() }, 1, MIX_ONLY);

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

	const perfMs = context.beginDeckLoad(3);
	spin(1);
	context.recordDeckLoad('deck-load sid=111111111111', { total: perfMs() }, 3, MIX_ONLY);
	stop();

	const row = lastDeckLoadRow();
	assert.equal(row.labels.load_avg_1m, '5.76');
	assert.equal(row.labels.mem_free_mb, '67.7');
	assert.equal(row.labels.swap_used_mb, '6535.4');
	assert.equal(row.labels.pressure, undefined, 'a real reading must not also say unknown');
	assert.ok(Number.isFinite(Number(row.labels.pressure_age_ms)));
});

test('a failed load records its conditions too', () => {
	const perfMs = context.beginDeckLoad(4);
	spin(1);
	context.recordDeckLoad('deck-load-fail', { failedAt: perfMs() }, 4, MIX_ONLY);

	const row = lastDeckLoadRow();
	assert.equal(row.kind, 'deck-load-fail');
	assert.equal(row.labels.solo, '1');
	assert.equal(row.labels.pressure, 'unknown');
});

test('the console line carries the labels, because that is where people grep', () => {
	const lines = [];
	console.info = (line) => lines.push(line);
	const perfMs = context.beginDeckLoad(1);
	spin(1);
	context.recordDeckLoad('deck-load sid=222222222222', { total: perfMs() }, 1, MIX_ONLY);
	assert.match(lines.at(-1), /solo=1/);
	assert.match(lines.at(-1), /concurrent_loads=1/);
	assert.match(lines.at(-1), /pressure=unknown/);
});
