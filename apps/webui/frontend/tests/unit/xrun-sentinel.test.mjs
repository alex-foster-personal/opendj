import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter, readFrontendSource as readSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * S1 / Q2: the only WebKit-safe glitch signal we can have.
 *
 * The shipping surface is WKWebView, where PerformanceObserver longtask, Long
 * Animation Frames and Compute Pressure are all unavailable. What IS available
 * is the audio thread timing itself: an AudioWorkletProcessor measuring wall
 * clock between its own `process()` callbacks (Paul Adenot's technique). Before
 * this, "0 xruns" was the S1 target and "instrumented today: NO" was the
 * status - an audible dropout during a set left no trace anywhere.
 *
 * The math lives in a pure module precisely because the detector runs where no
 * test can reach it. What is tested here is every decision it makes.
 *
 * Regression lines:
 * - if the threshold is derived from the render quantum rather than the device
 *   buffer then a healthy burst-rendering machine reports an xrun on every
 *   second callback and the counter is noise from the first second
 * - if a parked audio thread (hidden tab, suspended context) counts as an xrun
 *   then idling is indistinguishable from glitching
 * - if a parked gap can become worst_gap_ms then one hidden tab pins the worst
 *   case at tens of seconds for the rest of the session
 * - if the processor's inline rule drifts from classifyGapMs then the tested
 *   arithmetic is not the arithmetic that runs
 * - if a failed sentinel install is swallowed then the app reports zero xruns
 *   forever, which reads exactly like a healthy machine
 */

let xrun;

before(async () => {
	xrun = await loadTypeScriptModule('src/lib/rb/xrun-math.ts');
});

//-----------------------------------------------------------------------------
// the threshold
//-----------------------------------------------------------------------------

test('the render quantum duration is derived, never assumed', () => {
	assert.equal(xrun.RENDER_QUANTUM_FRAMES, 128);
	assert.equal(xrun.quantumDurationMs(128, 48000), (128 / 48000) * 1000);
	assert.ok(Math.abs(xrun.quantumDurationMs(128, 44100) - 2.902) < 0.001);
	assert.throws(() => xrun.quantumDurationMs(0, 44100), RangeError);
	assert.throws(() => xrun.quantumDurationMs(128, 0), RangeError);
});

test('SABOTAGE: the threshold clears the burst-render rhythm of a healthy machine', () => {
	// 256-frame device buffer at 44100Hz: the browser renders two 128-frame
	// quanta back to back and then sleeps ~5.8ms. A threshold at the quantum
	// (2.9ms) would call every second callback an xrun.
	const quantumMs = xrun.quantumDurationMs(128, 44100);
	const deviceBufferMs = (256 / 44100) * 1000;
	const threshold = xrun.xrunGapThresholdMs(quantumMs, deviceBufferMs);
	assert.ok(
		threshold > deviceBufferMs,
		`a ${deviceBufferMs}ms buffer period must not itself be reported as a dropout`
	);
	assert.equal(xrun.classifyGapMs(deviceBufferMs, threshold), 'ok');
	assert.equal(xrun.classifyGapMs(0.01, threshold), 'ok', 'a burst pair is not a glitch');
	assert.equal(xrun.classifyGapMs(deviceBufferMs * 3, threshold), 'xrun');
});

test('a context with no device yet falls back to the quantum, never to zero', () => {
	// baseLatency can read 0 before a device is attached. A zero threshold would
	// classify every callback as an xrun.
	const quantumMs = xrun.quantumDurationMs(128, 48000);
	const threshold = xrun.xrunGapThresholdMs(quantumMs, 0);
	assert.ok(threshold > quantumMs);
	assert.equal(xrun.classifyGapMs(quantumMs, threshold), 'ok');
});

test('a threshold factor at or below 1 is refused rather than shipped', () => {
	assert.throws(() => xrun.xrunGapThresholdMs(2.9, 5.8, 1), RangeError);
	assert.throws(() => xrun.xrunGapThresholdMs(2.9, 5.8, 0.5), RangeError);
	assert.throws(() => xrun.xrunGapThresholdMs(2.9, -1), RangeError);
	assert.throws(() => xrun.xrunGapThresholdMs(0, 5.8), RangeError);
});

//-----------------------------------------------------------------------------
// the classification and the tally
//-----------------------------------------------------------------------------

test('a parked audio thread is counted apart from a dropout, never as one', () => {
	const threshold = 10;
	assert.equal(xrun.classifyGapMs(9, threshold), 'ok');
	assert.equal(xrun.classifyGapMs(11, threshold), 'xrun');
	assert.equal(xrun.classifyGapMs(xrun.XRUN_PARKED_GAP_MS, threshold), 'parked');
	assert.equal(xrun.classifyGapMs(40_000, threshold), 'parked');
	assert.throws(() => xrun.classifyGapMs(-1, threshold), RangeError);
	assert.throws(() => xrun.classifyGapMs(Number.NaN, threshold), RangeError);
});

test('the tally counts callbacks, xruns and parked gaps as three separate things', () => {
	// Starts at undefined on purpose: the fold owns the one empty tally, so a
	// caller cannot begin from a hand-written literal that drifted from it.
	let tally;
	for (const gapMs of [1, 2, 1, 30, 2, 1, 45, 90_000, 1]) {
		tally = xrun.foldXrunGap(tally, gapMs, 10);
	}
	assert.equal(tally.callbacks, 9);
	assert.equal(tally.xruns, 2, 'only the 30ms and 45ms gaps crossed the 10ms threshold');
	assert.equal(tally.parked, 1);
	assert.equal(
		tally.worstGapMs,
		45,
		'SABOTAGE: if a parked 90s gap can become the worst gap then one hidden tab pins ' +
			'the worst case for the whole session and the number stops meaning anything'
	);
});

test('the tally is a pure fold: the input is never mutated', () => {
	const start = xrun.foldXrunGap(undefined, 1, 10);
	const next = xrun.foldXrunGap(start, 50, 10);
	assert.equal(start.xruns, 0);
	assert.equal(next.xruns, 1);
	assert.notEqual(start, next);
});

//-----------------------------------------------------------------------------
// the port message and the session counter
//-----------------------------------------------------------------------------

function report(overrides = {}) {
	return {
		xruns: 3,
		parked: 1,
		callbacks: 740,
		worst_gap_ms: 42.5,
		window_ms: 2000,
		threshold_ms: 9.7,
		clock: 'performance',
		...overrides
	};
}

test('a port message this build cannot read is rejected, not folded', () => {
	assert.ok(xrun.isXrunReport(report()));
	assert.ok(!xrun.isXrunReport(null));
	assert.ok(!xrun.isXrunReport('3'));
	assert.ok(!xrun.isXrunReport(report({ xruns: -1 })));
	assert.ok(!xrun.isXrunReport(report({ callbacks: Number.NaN })));
	assert.ok(!xrun.isXrunReport(report({ clock: '' })));
	const { worst_gap_ms: _dropped, ...missing } = report();
	assert.ok(!xrun.isXrunReport(missing));
});

test('the session counter accumulates and keeps the single worst gap', () => {
	let session = xrun.EMPTY_XRUN_SESSION;
	session = xrun.foldXrunReport(session, report());
	session = xrun.foldXrunReport(session, report({ xruns: 1, worst_gap_ms: 12, parked: 0 }));
	assert.equal(session.xruns, 4);
	assert.equal(session.parked, 1);
	assert.equal(session.reports, 2);
	assert.equal(session.callbacks, 1480);
	assert.equal(session.worst_gap_ms, 42.5, 'the worst gap is a max, never the latest value');
});

test('HONEST DENOMINATORS: the message names its window and its callback count', () => {
	const message = xrun.xrunReportMessage(report());
	assert.ok(message.includes('3 xrun'), 'the count');
	assert.ok(message.includes('2000ms'), 'over what window');
	assert.ok(message.includes('740'), 'against how many callbacks');
	assert.ok(message.includes('42.5'), 'the worst gap');
	assert.ok(message.includes('9.7'), 'and the threshold it was judged against');
	assert.ok(message.includes('parked'), 'and what was excluded');
});

//-----------------------------------------------------------------------------
// wiring: the processor must run the arithmetic that was tested
//-----------------------------------------------------------------------------

test('SABOTAGE: the worklet inlines the same rule the pure module was tested on', () => {
	// The processor cannot import, so the two comparisons exist twice. Pinned
	// against each other: if either side is edited alone, the arithmetic under
	// test stops being the arithmetic that runs on the audio thread.
	const processor = readSource('src/lib/rb/xrun-sentinel-processor.js');
	const math = readSource('src/lib/rb/xrun-math.ts');
	assert.ok(math.includes('if (gapMs >= parkedGapMs) return'), 'the parked rule moved');
	assert.ok(math.includes('if (gapMs > thresholdMs) return'), 'the xrun rule moved');
	assert.ok(
		processor.includes('if (gapMs >= this.parkedGapMs) {'),
		'the worklet parked rule must match classifyGapMs (>=), or a parked gap is counted ' +
			'as a dropout on the audio thread while the tested module says otherwise'
	);
	assert.ok(
		processor.includes('} else if (gapMs > this.thresholdMs) {'),
		'the worklet xrun rule must match classifyGapMs (>), and must be an ELSE of the ' +
			'parked branch so a parked gap can never also be counted as an xrun'
	);
	assert.ok(
		processor.includes('if (gapMs > this.worstGapMs) this.worstGapMs = gapMs;'),
		'the worst gap must be tracked inside the xrun branch only'
	);
});

test('the worklet holds no policy: every threshold arrives from the main thread', () => {
	const processor = readSource('src/lib/rb/xrun-sentinel-processor.js');
	assert.ok(processor.includes('processorOptions'), 'thresholds are injected, not hardcoded');
	for (const constant of ['XRUN_GAP_FACTOR', 'XRUN_PARKED_GAP_MS', 'XRUN_REPORT_INTERVAL_MS']) {
		assert.ok(
			!processor.includes(constant),
			`${constant} must stay on the main thread; a second copy in the worklet is a ` +
				'threshold nobody can see or test'
		);
	}
	assert.ok(
		processor.includes('throw new RangeError('),
		'a missing threshold must fail at construction rather than counting against NaN, ' +
			'which classifies every callback as healthy forever'
	);
	assert.ok(
		processor.includes("registerProcessor('mdt-xrun-sentinel'"),
		'the registered name must match XRUN_SENTINEL_PROCESSOR_NAME'
	);
	const sentinel = readSource('src/lib/rb/xrun-sentinel.ts');
	assert.ok(sentinel.includes("XRUN_SENTINEL_PROCESSOR_NAME = 'mdt-xrun-sentinel'"));
});

test('the sentinel writes no audio, and is muted on the way out anyway', () => {
	const processor = readSource('src/lib/rb/xrun-sentinel-processor.js');
	assert.ok(
		!processor.includes('outputs['),
		'the sentinel must never touch its output buffer; a measurement instrument that can ' +
			'emit a sample is a way to put noise into a live set'
	);
	const sentinel = readSource('src/lib/rb/xrun-sentinel.ts');
	assert.ok(
		sentinel.includes('mute.gain.value = 0;'),
		'the node is connected only to be scheduled; a zero gain means even a bug in the ' +
			'processor cannot reach the master bus'
	);
});

test('a healthy window costs nothing: no post, no ring row', () => {
	const processor = readSource('src/lib/rb/xrun-sentinel-processor.js');
	assert.ok(
		processor.includes('if (this.xruns > 0 || this.parked > 0) {'),
		'an idle set must produce zero MessagePort traffic, or the instrument becomes the ' +
			'load it is measuring'
	);
	const sentinel = readSource('src/lib/rb/xrun-sentinel.ts');
	assert.ok(
		sentinel.includes("recordPerfEvent('xrun'"),
		'a report that reaches the main thread must land in the ring'
	);
});

test('the graph arms the sentinel without ever letting it break audio', () => {
	const body = engineBlockAfter('export function armXrunSentinel(ctx: AudioContext): void {');
	assert.ok(
		body.includes('.catch('),
		'addModule is async and can reject; an unhandled rejection here would surface as a ' +
			'page error in the e2e suites'
	);
	assert.ok(
		body.includes("recordPerfEvent(\n\t\t\t'xrun-sentinel-failed'"),
		'SABOTAGE: a swallowed install failure leaves the app reporting zero xruns forever, ' +
			'which is indistinguishable from a healthy machine'
	);
	const graph = engineBlockAfter('function _ensureGraph(): AudioContext {');
	assert.ok(
		graph.includes('armXrunSentinel(_ctx);'),
		'the sentinel must be armed with the graph, or it only exists in theory'
	);
	const armAt = graph.indexOf('armXrunSentinel(_ctx);');
	const returnAt = graph.lastIndexOf('return _ctx;');
	assert.ok(
		armAt !== -1 && armAt < returnAt,
		'arming must not sit after the return, and must not be awaited: the decks may not ' +
			'wait on an instrument'
	);
});

test('the session counter is reachable without a UI', () => {
	const sentinel = readSource('src/lib/rb/xrun-sentinel.ts');
	assert.ok(
		sentinel.includes('export function readXrunSessionCounter()'),
		'agent-native parity: the counter needs a programmatic read, not only a meter'
	);
	assert.ok(sentinel.includes('__mdtXruns'), 'and a DevTools handle alongside __mdtPerfLog');
	assert.ok(
		sentinel.includes('export function flushXrunSessionCounter()'),
		'an agent must be able to flush the worklet window before reading a pressure boundary'
	);
	assert.ok(sentinel.includes('__mdtFlushXruns'), 'the acknowledged flush needs a DevTools handle');
	const processor = readSource('src/lib/rb/xrun-sentinel-processor.js');
	assert.ok(processor.includes("kind: 'xrun-flush-ack'"), 'the worklet must acknowledge a flush');
	assert.ok(processor.includes('judging: this.judging'), 'the acknowledgement must expose warmup readiness');
	assert.ok(
		sentinel.includes("xrun sentinel has not completed cadence warmup"),
		'a flush before cadence warmup must reject rather than claim an authoritative zero'
	);
});
