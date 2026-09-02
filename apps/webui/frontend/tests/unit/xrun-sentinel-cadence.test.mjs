import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * P0 (audio-never-cuts-out-under-thrash, defect D3): the glitch counter must
 * count glitches, and only glitches.
 *
 * On Wed 2 Sep 2026 the shipping sentinel reported
 *
 *   173 xrun(s) in 2009ms over 692 audio callbacks; worst gap 12-14ms against
 *   a 5.354ms late-callback threshold
 *
 * on a machine that was, at that moment, playing perfectly. 692 / 4 = 173 is
 * not a coincidence: WebKit renders four 128-frame quanta back to back to fill
 * one 512-frame HAL buffer and then sleeps ~11.6ms, so EVERY FOURTH gap is the
 * normal cadence of a healthy device and the sentinel called all of them
 * dropouts.
 *
 * The arithmetic in xrun-math.ts is not the bug - test 3 below proves it
 * classifies both streams correctly when handed the right number. The bug is
 * the INPUT: `installXrunSentinel` derives the threshold from
 * `AudioContext.baseLatency`, and on this device baseLatency reported roughly
 * one render quantum rather than the true 512-frame buffer period. The
 * threshold has to come from the cadence the audio thread ACTUALLY exhibits.
 *
 * DEVICE-AGNOSTIC BY CONSTRUCTION. Nothing here mentions a headphone. A device
 * is three numbers - sample rate, HAL buffer size, and what baseLatency claims
 * - and the same rule is asserted for every row of the table. The row that
 * fails is the CLASS of device whose baseLatency under-reports its buffer, not
 * one product.
 *
 * Regression lines:
 * - if a healthy burst-render cadence reports any xruns then the counter is
 *   noise from the first second and nobody will ever act on it
 * - if a genuine stall on that same cadence is not exactly one xrun then the
 *   number cannot be reasoned about at all
 * - if the threshold is derived from baseLatency rather than from the observed
 *   callback cadence then any device that under-reports its buffer is
 *   permanently mis-measured
 */

const PROCESSOR_PATH = 'src/lib/rb/xrun-sentinel-processor.js';
const RENDER_QUANTUM_FRAMES = 128;

/** Gaps inside one burst group are essentially zero; a real one is not. */
const INTRA_GROUP_GAP_MS = 0.05;

let xrun;
let ProcessorClass;

//-----------------------------------------------------------------------------
// harness: the worklet scope, faked well enough to run the real processor
//-----------------------------------------------------------------------------

/** The wall clock the processor reads. Advanced by the stream driver below. */
let _clockMs = 0;

/**
 * Load `xrun-sentinel-processor.js` for real.
 *
 * The file cannot be imported: it has no exports, and it references
 * `AudioWorkletProcessor` / `registerProcessor` / `currentTime` / `sampleRate`,
 * which exist only inside an AudioWorkletGlobalScope. So the globals are
 * installed and the source is evaluated - the processor that runs here is the
 * one that ships, byte for byte, rather than a re-implementation of it that
 * could quietly disagree with the file under test.
 */
function _installWorkletScope() {
	Object.defineProperty(globalThis, 'performance', {
		configurable: true,
		writable: true,
		value: { now: () => _clockMs }
	});
	globalThis.AudioWorkletProcessor = class {
		constructor() {
			this.port = { postMessage: (message) => this.__posts.push(message) };
			this.__posts = [];
		}
	};
	let registered = null;
	globalThis.registerProcessor = (name, ctor) => {
		registered = { name, ctor };
	};
	globalThis.currentTime = 0;
	globalThis.sampleRate = 44100;
	// eslint-disable-next-line no-new-func -- the point is to run the shipped text
	new Function(readFrontendSource(PROCESSOR_PATH))();
	assert.ok(
		registered !== null && registered.name === 'mdt-xrun-sentinel',
		'if the processor no longer registers itself as mdt-xrun-sentinel then this ' +
			'harness is driving nothing and every assertion below is vacuous'
	);
	return registered.ctor;
}

/**
 * The inter-callback gaps a device of this shape produces when it is HEALTHY.
 *
 * One "group" is however many 128-frame render quanta it takes to fill the HAL
 * buffer. The browser renders them back to back and then sleeps for the rest of
 * the buffer period, so the gaps are [~0, ~0, ~0, period] and NOT [period/4] x4.
 */
function healthyCadenceGapsMs({ sampleRateHz, halBufferFrames }, groups) {
	const quantaPerGroup = halBufferFrames / RENDER_QUANTUM_FRAMES;
	assert.ok(
		Number.isInteger(quantaPerGroup) && quantaPerGroup >= 1,
		`a HAL buffer of ${halBufferFrames} frames is not a whole number of render quanta`
	);
	const periodMs = (halBufferFrames / sampleRateHz) * 1000;
	const tailMs = periodMs - INTRA_GROUP_GAP_MS * (quantaPerGroup - 1);
	const gaps = [];
	for (let group = 0; group < groups; group += 1) {
		for (let q = 0; q < quantaPerGroup; q += 1) {
			gaps.push(q < quantaPerGroup - 1 ? INTRA_GROUP_GAP_MS : tailMs);
		}
	}
	return gaps;
}

/** Drive the real processor over a gap stream and total up what it posted. */
function runSentinel(gapsMs, thresholdMs) {
	_clockMs = 0;
	const processor = new ProcessorClass({
		processorOptions: {
			thresholdMs,
			parkedGapMs: xrun.XRUN_PARKED_GAP_MS,
			reportIntervalMs: xrun.XRUN_REPORT_INTERVAL_MS
		}
	});
	processor.process();
	for (const gapMs of gapsMs) {
		_clockMs += gapMs;
		processor.process();
	}
	// Force the tail window out, so a stream shorter than the report interval
	// is still observable rather than silently swallowed.
	_clockMs += xrun.XRUN_REPORT_INTERVAL_MS;
	processor.process();
	const totals = processor.__posts.reduce(
		(total, report) => ({
			xruns: total.xruns + report.xruns,
			parked: total.parked + report.parked,
			callbacks: total.callbacks + report.callbacks,
			worstGapMs: Math.max(total.worstGapMs, report.worst_gap_ms)
		}),
		{ xruns: 0, parked: 0, callbacks: 0, worstGapMs: 0 }
	);
	// The raw reports travel too: one report is what an operator actually sees in
	// the ring, and summing them would hide which window a number came from.
	return { ...totals, reports: processor.__posts };
}

/** Exactly the number `installXrunSentinel` feeds the processor today. */
function shippedThresholdMs({ sampleRateHz, reportedBaseLatencyFrames }) {
	return xrun.xrunGapThresholdMs(
		xrun.quantumDurationMs(RENDER_QUANTUM_FRAMES, sampleRateHz),
		(reportedBaseLatencyFrames / sampleRateHz) * 1000
	);
}

/**
 * Three devices, one rule. Only the third differs in kind: its baseLatency
 * under-reports the buffer the audio thread is really being driven at, which is
 * the whole failure - not the brand of the device that did it.
 */
const DEVICES = [
	{
		name: 'HAL buffer 128, baseLatency honest',
		sampleRateHz: 44100,
		halBufferFrames: 128,
		reportedBaseLatencyFrames: 128
	},
	{
		name: 'HAL buffer 256, baseLatency honest',
		sampleRateHz: 44100,
		halBufferFrames: 256,
		reportedBaseLatencyFrames: 256
	},
	{
		name: 'HAL buffer 512, baseLatency under-reports it as one quantum',
		sampleRateHz: 44100,
		halBufferFrames: 512,
		reportedBaseLatencyFrames: 128
	}
];

before(async () => {
	xrun = await loadTypeScriptModule('src/lib/rb/xrun-math.ts');
	ProcessorClass = _installWorkletScope();
});

//-----------------------------------------------------------------------------
// the false positive
//-----------------------------------------------------------------------------

test('a healthy device cadence reports zero xruns, whatever its buffer size', () => {
	for (const device of DEVICES) {
		const gaps = healthyCadenceGapsMs(device, 200);
		const observed = runSentinel(gaps, shippedThresholdMs(device));
		assert.equal(
			observed.xruns,
			0,
			`if a healthy ${device.name} reports ${observed.xruns} xrun(s) in ` +
				`${observed.callbacks} callbacks then broken - the burst-render rhythm of a ` +
				'perfectly working machine is being counted as dropouts, which is what the ' +
				'shipping sentinel did on Wed 2 Sep 2026 (173 in 692), and a counter that ' +
				'cries wolf every fourth callback is a counter nobody will believe when the ' +
				'audio really does stop'
		);
	}
});

test('the live Wed 2 Sep 2026 report is reproduced exactly, so the fixture is honest', () => {
	// Not an acceptance criterion - a proof that the stream above IS the machine
	// that failed, rather than a scenario invented to make a point. If this
	// stops matching, the other tests here are arguing about a different device.
	const device = DEVICES[2];
	const observed = runSentinel(healthyCadenceGapsMs(device, 200), shippedThresholdMs(device));
	// The FIRST posted window, not the sum: the live line quoted one 2009ms window.
	const first = observed.reports[0];
	assert.equal(Math.round(shippedThresholdMs(device) * 1000) / 1000, 5.354);
	assert.equal(first.xruns, 173);
	assert.equal(first.callbacks, 692);
	assert.ok(Math.abs(first.window_ms - 2009) < 1);
	assert.ok(Math.abs(first.worst_gap_ms - 11.46) < 0.1);
});

//-----------------------------------------------------------------------------
// the true positive it must not lose
//-----------------------------------------------------------------------------

test('one genuine 30ms stall on that same cadence is exactly one xrun', () => {
	const device = DEVICES[2];
	const gaps = healthyCadenceGapsMs(device, 200);
	gaps.splice(Math.floor(gaps.length / 2), 0, 30);
	const observed = runSentinel(gaps, shippedThresholdMs(device));
	assert.equal(
		observed.xruns,
		1,
		`if one real 30ms stall counts as ${observed.xruns} xrun(s) then broken - the ` +
			'true positive is buried in false ones and the count carries no information'
	);
	assert.ok(
		Math.abs(observed.worstGapMs - 30) < 0.001,
		'if the worst gap is not the 30ms stall then broken - the one number an operator ' +
			'would quote is describing normal cadence instead of the dropout'
	);
});

//-----------------------------------------------------------------------------
// the control: the arithmetic is fine, the input is not
//-----------------------------------------------------------------------------

test('CONTROL: handed the observed cadence, the shipped arithmetic is already correct', () => {
	// This one PASSES today, and that is the finding. classifyGapMs, the fold and
	// the processor loop all behave; xrunGapThresholdMs behaves. Nothing here
	// needs rewriting. What must change is WHERE the second argument comes from.
	const device = DEVICES[2];
	const cadencePeriodMs = (device.halBufferFrames / device.sampleRateHz) * 1000;
	const threshold = xrun.xrunGapThresholdMs(
		xrun.quantumDurationMs(RENDER_QUANTUM_FRAMES, device.sampleRateHz),
		cadencePeriodMs
	);
	const healthy = runSentinel(healthyCadenceGapsMs(device, 200), threshold);
	assert.equal(healthy.xruns, 0, 'if the cadence-derived threshold still flags a healthy stream then broken');

	const stalled = healthyCadenceGapsMs(device, 200);
	stalled.splice(Math.floor(stalled.length / 2), 0, 30);
	assert.equal(
		runSentinel(stalled, threshold).xruns,
		1,
		'if the cadence-derived threshold hides a real 30ms stall then broken - the fix ' +
			'must not buy silence by raising the threshold past the failures it exists to see'
	);
});

//-----------------------------------------------------------------------------
// the contract the fix owes
//-----------------------------------------------------------------------------

test('the threshold is derived from the observed callback cadence, not from baseLatency', () => {
	assert.equal(
		typeof xrun.xrunThresholdFromCadenceMs,
		'function',
		'if xrun-math exposes no cadence-derived threshold then broken - the only inputs ' +
			'available today are baseLatency (which under-reported a 512-frame buffer as ' +
			'one 128-frame quantum) and the render quantum itself, and both are smaller ' +
			'than the period the audio thread is actually driven at'
	);
	const device = DEVICES[2];
	const gaps = healthyCadenceGapsMs(device, 20);
	const derived = xrun.xrunThresholdFromCadenceMs(gaps);
	const cadencePeriodMs = (device.halBufferFrames / device.sampleRateHz) * 1000;
	assert.ok(
		derived > cadencePeriodMs,
		`if the derived threshold (${derived}ms) does not clear the observed cadence ` +
			`period (${cadencePeriodMs}ms) then broken`
	);
	assert.equal(
		runSentinel(gaps, derived).xruns,
		0,
		'if a threshold derived from a stream still flags that same stream then broken'
	);
});
