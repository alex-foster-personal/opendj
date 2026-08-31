import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, mock, test } from 'node:test';

import { readFrontendSource as readSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Q2 / PERF-R4: a distribution underneath the 5000ms Signalsmith cliff.
 *
 * Every worklet command goes through `StretchCommandGate.run`, which races it
 * against `withStretchCommandTimeout`. Nothing measured how long a normal
 * schedule / addBuffers / latency ack took, so the packaged-app
 * "Signalsmith schedule timed out after 5000ms" failures arrive with no
 * distribution beneath them: we cannot see 2ms drifting toward 5000ms.
 *
 * The instrument must not become the problem. The perf ring is 40 rows; one
 * pitch-fader drag posts a schedule per pointermove, so a row per command
 * evicts the entire ring and destroys the deck-load evidence it exists to
 * hold. Hence: ONE aggregated row per flush window, plus an immediate row for
 * a single ack that crossed the slow threshold.
 *
 * Regression lines:
 * - if a row is emitted per command then the ring floods and every other
 *   diagnosis is evicted by a knob drag
 * - if the aggregate stops carrying p95/max then the cliff still has no
 *   distribution under it and the instrument is decorative
 * - if a slow ack waits for the window then the one event worth an alert
 *   arrives up to 5s late
 * - if a timed-out command is excluded from the window then the max never
 *   shows the cliff being approached
 */

let stats;
let infoLines;
let warnLines;
let realInfo;
let realWarn;

before(async () => {
	stats = await loadTypeScriptModule('src/lib/rb/worklet-ack-stats.ts');
});

beforeEach(() => {
	stats.resetWorkletAckStats();
	infoLines = [];
	warnLines = [];
	realInfo = console.info;
	realWarn = console.warn;
	console.info = (line) => infoLines.push(String(line));
	console.warn = (line) => warnLines.push(String(line));
	mock.timers.enable({ apis: ['setTimeout'] });
});

afterEach(() => {
	mock.timers.reset();
	console.info = realInfo;
	console.warn = realWarn;
	stats.resetWorkletAckStats();
});

/** The `[perf] worklet-ack ...` lines recorded so far. */
function ackRows() {
	return infoLines.filter((line) => line.startsWith('[perf] worklet-ack '));
}

/** Parse one `k=v k=v` perf line into a stage map. */
function parseStages(line) {
	const stages = {};
	for (const token of line.split(/\s+/)) {
		const eq = token.indexOf('=');
		if (eq <= 0) continue;
		const value = Number(token.slice(eq + 1));
		if (Number.isFinite(value)) stages[token.slice(0, eq)] = value;
	}
	return stages;
}

//-----------------------------------------------------------------------------
// the pure arithmetic
//-----------------------------------------------------------------------------

test('nearest-rank percentiles pick a real sample, never an interpolated one', () => {
	const sorted = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
	assert.equal(stats.percentileMs(sorted, 0.5), 5);
	assert.equal(stats.percentileMs(sorted, 0.95), 10);
	assert.equal(stats.percentileMs([7], 0.5), 7);
	assert.equal(stats.percentileMs([7], 0.95), 7);
	// Nearest rank at n=20 puts p95 on the 19th sample, so ONE cliff among 19
	// fast acks does not reach it. That is correct and it is exactly why the row
	// also carries max: p95 answers "is this machine healthy", max answers "did
	// anything touch the cliff at all". A summary with only p95 would be blind
	// to the single worst command in a window, which is the one worth seeing.
	const oneCliff = [...Array.from({ length: 19 }, () => 2), 5000];
	assert.equal(stats.percentileMs(oneCliff, 0.5), 2);
	assert.equal(stats.percentileMs(oneCliff, 0.95), 2);
	assert.equal(oneCliff[oneCliff.length - 1], 5000, 'max is the term that catches it');
	// Two in twenty does reach p95, so a machine genuinely drifting toward the
	// timeout shows up in the percentile and not only in the outlier term.
	const twoCliffs = [...Array.from({ length: 18 }, () => 2), 5000, 5000];
	assert.equal(stats.percentileMs(twoCliffs, 0.95), 5000);
});

test('a percentile of nothing is refused rather than reported as zero', () => {
	assert.throws(() => stats.percentileMs([], 0.5), RangeError);
	assert.throws(() => stats.percentileMs([1, 2], 0), RangeError);
	assert.throws(() => stats.percentileMs([1, 2], 1.5), RangeError);
});

test('operation names become stable snake_case stage keys', () => {
	assert.equal(stats.ackOpKey('schedule'), 'schedule');
	assert.equal(stats.ackOpKey('add buffers'), 'add_buffers');
	assert.equal(stats.ackOpKey('position update configuration'), 'position_update_configuration');
	assert.equal(stats.ackOpKey('processor creation'), 'processor_creation');
	assert.throws(() => stats.ackOpKey('   '), RangeError);
});

test('the window summarizes per operation and keeps the operations apart', () => {
	const window = new stats.WorkletAckWindow();
	for (const ms of [1, 2, 3, 4]) window.record('schedule', ms);
	window.record('add buffers', 900);
	assert.equal(window.sampleCount, 5);
	const row = window.summarize();
	assert.equal(row.total_n, 5);
	assert.equal(row.schedule_n, 4);
	assert.equal(row.schedule_p50_ms, 2);
	assert.equal(row.schedule_p95_ms, 4);
	assert.equal(row.schedule_max_ms, 4);
	assert.equal(row.add_buffers_n, 1);
	assert.equal(row.add_buffers_max_ms, 900);
	assert.ok(
		row.add_buffers_max_ms !== row.schedule_max_ms,
		'if operations are pooled then one slow addBuffers hides behind a thousand fast schedules'
	);
	for (const [name, value] of Object.entries(row)) {
		assert.equal(typeof value, 'number', `${name} must be a number for the stages contract`);
		assert.ok(Number.isFinite(value), `${name} must be finite, got ${value}`);
	}
});

test('an empty window summarizes to nothing, so a silent period emits no row', () => {
	const window = new stats.WorkletAckWindow();
	assert.equal(window.sampleCount, 0);
	assert.deepEqual(window.summarize(), {});
});

test('a non-finite or negative ack duration is refused, not averaged in', () => {
	const window = new stats.WorkletAckWindow();
	assert.throws(() => window.record('schedule', Number.NaN), RangeError);
	assert.throws(() => window.record('schedule', -1), RangeError);
	assert.doesNotThrow(() => window.record('schedule', 0));
});

//-----------------------------------------------------------------------------
// the emit policy: one row per window, never one per command
//-----------------------------------------------------------------------------

test('SABOTAGE: a hundred acks produce one ring row, not a hundred', () => {
	for (let i = 0; i < 100; i += 1) stats.recordWorkletAck('schedule', 2 + (i % 3), true);
	assert.deepEqual(
		ackRows(),
		[],
		'nothing may be emitted while commands are still flowing, or a pitch-fader drag ' +
			'evicts the whole 40-row ring one schedule at a time'
	);
	mock.timers.tick(stats.WORKLET_ACK_FLUSH_MS);
	const rows = ackRows();
	assert.equal(rows.length, 1, `expected exactly one aggregated row, got ${rows.length}`);
	const stages = parseStages(rows[0]);
	assert.equal(stages.total_n, 100);
	assert.equal(stages.schedule_n, 100);
	assert.equal(stages.schedule_max_ms, 4);
});

test('a window with no commands in it emits nothing at all', () => {
	mock.timers.tick(stats.WORKLET_ACK_FLUSH_MS * 4);
	assert.deepEqual(ackRows(), []);
});

test('commands after a flush open a new window rather than re-reporting the old one', () => {
	stats.recordWorkletAck('schedule', 2, true);
	mock.timers.tick(stats.WORKLET_ACK_FLUSH_MS);
	assert.equal(ackRows().length, 1);

	stats.recordWorkletAck('latency query', 7, true);
	assert.equal(ackRows().length, 1, 'the second window must not emit early');
	mock.timers.tick(stats.WORKLET_ACK_FLUSH_MS);
	const rows = ackRows();
	assert.equal(rows.length, 2);
	const second = parseStages(rows[1]);
	assert.equal(second.total_n, 1);
	assert.equal(second.latency_query_max_ms, 7);
	assert.equal(
		second.schedule_n,
		undefined,
		'if the previous window survives the flush then every later row double-counts it'
	);
});

test('a single slow ack is reported immediately, not up to a window later', () => {
	stats.recordWorkletAck('schedule', stats.WORKLET_ACK_SLOW_MS + 1, true);
	const slow = warnLines.filter((line) => line.includes('worklet-ack-slow'));
	assert.equal(slow.length, 1, 'the one event worth an alert must not wait for the window');
	assert.ok(slow[0].includes('schedule'), 'the event must name the operation');
	assert.ok(slow[0].includes(String(stats.WORKLET_ACK_SLOW_MS)), 'and the threshold it crossed');
	assert.deepEqual(ackRows(), [], 'a slow ack must not also short-circuit the aggregate');
	mock.timers.tick(stats.WORKLET_ACK_FLUSH_MS);
	assert.equal(
		parseStages(ackRows()[0]).schedule_n,
		1,
		'the slow sample belongs in the distribution as well as in its own event'
	);
});

test('an ack at the threshold is not slow; the one above it is', () => {
	stats.recordWorkletAck('schedule', stats.WORKLET_ACK_SLOW_MS, true);
	assert.deepEqual(warnLines.filter((line) => line.includes('worklet-ack-slow')), []);
	stats.recordWorkletAck('schedule', stats.WORKLET_ACK_SLOW_MS + 0.001, true);
	assert.equal(warnLines.filter((line) => line.includes('worklet-ack-slow')).length, 1);
});

test('a FAILED command still contributes its duration, and says so', () => {
	// A 5000ms timeout is the most informative sample in the set: excluded, the
	// max never shows the cliff being reached.
	stats.recordWorkletAck('schedule', 5000, false);
	const slow = warnLines.filter((line) => line.includes('worklet-ack-slow'));
	assert.equal(slow.length, 1);
	assert.ok(
		slow[0].includes('failed'),
		'a failed ack must be distinguishable from a merely slow one in the event text'
	);
	mock.timers.tick(stats.WORKLET_ACK_FLUSH_MS);
	assert.equal(parseStages(ackRows()[0]).schedule_max_ms, 5000);
});

test('the aggregated row keeps the [perf] console contract intact', () => {
	stats.recordWorkletAck('add buffers', 12.5, true);
	mock.timers.tick(stats.WORKLET_ACK_FLUSH_MS);
	const row = ackRows()[0];
	assert.ok(
		row.startsWith('[perf] worklet-ack '),
		'tests/e2e/webkit-deckload.spec.ts parses every line starting `[perf] `; the kind ' +
			'must sit where deck-load sits'
	);
	assert.ok(!row.includes('deck='), 'worklet acks are not per-deck; a deck bit would be a lie');
});

//-----------------------------------------------------------------------------
// wiring: every worklet command must pass through the instrument
//-----------------------------------------------------------------------------

test('the command gate times every worklet round trip, success or failure', () => {
	const adapter = readSource('src/lib/rb/stretch-adapter.ts');
	assert.ok(
		adapter.includes('recordWorkletAck('),
		'if the gate does not record then no worklet command is measured at all'
	);
	const runAt = adapter.indexOf('async run<T>(operation: string');
	assert.ok(runAt !== -1);
	const runBody = adapter.slice(runAt, adapter.indexOf('\n\t}\n}', runAt));
	assert.ok(
		runBody.includes('performance.now()'),
		'the gate must take its own start stamp; there is no other clock on this path'
	);
	assert.equal(
		(runBody.match(/recordWorkletAck\(/g) ?? []).length,
		2,
		'both the resolved and the rejected path must record, or a timeout is invisible ' +
			'in the very distribution built to see timeouts coming'
	);
});

test('processor creation is timed too, since it owns the 15000ms cliff', () => {
	const adapter = readSource('src/lib/rb/stretch-adapter.ts');
	assert.ok(
		adapter.includes("'processor creation'"),
		'the create timeout is a separate cliff and needs its own distribution'
	);
	const createAt = adapter.indexOf('static async create(');
	const createBody = adapter.slice(createAt, adapter.indexOf('\n\tconnect(', createAt));
	assert.ok(
		createBody.includes('recordWorkletAck('),
		'create bypasses StretchCommandGate.run and would otherwise be the one unmeasured ' +
			'worklet round trip - and it is the slowest'
	);
});

test('SABOTAGE: the adapter never emits a per-command ring row', () => {
	const adapter = readSource('src/lib/rb/stretch-adapter.ts');
	assert.ok(
		!adapter.includes('recordPerfTiming('),
		'a recordPerfTiming call in the adapter means a row per worklet command; one ' +
			'pitch-fader drag would then evict the entire 40-row ring'
	);
});
