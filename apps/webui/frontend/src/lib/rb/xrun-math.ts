/**
 * S1 / Q2: the arithmetic of the audio-thread glitch detector.
 *
 * WHY THIS EXISTS AS A SEPARATE MODULE. The detector itself has to live inside
 * an `AudioWorkletProcessor`, where there is no test harness, no import graph,
 * and no way to observe anything except through a MessagePort. So every
 * decision it makes is defined here as a total function of its arguments, and
 * the processor carries only the loop that feeds them. The main-thread side
 * folds the reports with the same functions.
 *
 * THE TECHNIQUE (Paul Adenot). WebKit exposes none of the Chromium-only
 * long-task or pressure APIs, so the only WebKit-safe glitch signal available
 * to us is the audio thread timing ITSELF: an `AudioWorkletProcessor` measures
 * wall clock between successive `process()` callbacks and compares it against
 * what the device should be asking for. A callback that arrives late means the
 * audio thread missed its deadline, which is what an operator hears as a
 * dropout. Today the app has NO glitch detection of any kind (spec section 1,
 * S1: "0 xruns" with "instrumented today: NO").
 *
 * WHY THE THRESHOLD IS THE DEVICE BUFFER AND NOT THE RENDER QUANTUM. Callbacks
 * are not evenly spaced. Web Audio renders in 128-frame quanta, but the browser
 * renders as many quanta as it takes to fill one device buffer and then sleeps,
 * so a healthy 256-frame device buffer at 44100Hz produces gaps of roughly
 * [0, 5.8, 0, 5.8]ms, not [2.9, 2.9, 2.9, 2.9]ms. Comparing each gap against
 * the quantum would therefore report an xrun on every second callback of a
 * perfectly healthy machine. The buffer period (`AudioContext.baseLatency`) is
 * the real ceiling, and the factor above it is the slack a healthy scheduler
 * is allowed.
 *
 * WHY A PARKED CEILING. A hidden tab, a suspended context or a laptop lid
 * produces gaps of seconds. Those are not dropouts an operator heard; counting
 * them would make the xrun total useless exactly on the machines that idle
 * most. They are counted separately, so the number that is dropped is still
 * visible rather than silently discarded.
 */

/** A healthy gap may exceed the device buffer period by this factor. */
export const XRUN_GAP_FACTOR = 1.5;

/** Absolute slack on top of the factor, for very small device buffers. */
export const XRUN_GAP_FLOOR_MS = 1;

/** A gap this large is a parked audio thread, not a dropout. */
export const XRUN_PARKED_GAP_MS = 500;

/** The processor reports at most this often, and only when it has something. */
export const XRUN_REPORT_INTERVAL_MS = 2_000;

/** Web Audio renders in fixed 128-frame quanta on every implementation. */
export const RENDER_QUANTUM_FRAMES = 128;

export type XrunGapKind = 'ok' | 'xrun' | 'parked';

export interface XrunTally {
	callbacks: number;
	xruns: number;
	parked: number;
	worstGapMs: number;
}

/** One window's worth of observations, as it crosses the MessagePort. */
export interface XrunReport {
	xruns: number;
	parked: number;
	callbacks: number;
	worst_gap_ms: number;
	window_ms: number;
	threshold_ms: number;
	/** Which wall clock the worklet scope actually had. */
	clock: string;
}

export interface XrunSessionCounter {
	xruns: number;
	parked: number;
	reports: number;
	callbacks: number;
	worst_gap_ms: number;
}

export const EMPTY_XRUN_TALLY: Readonly<XrunTally> = Object.freeze({
	callbacks: 0,
	xruns: 0,
	parked: 0,
	worstGapMs: 0
});

export const EMPTY_XRUN_SESSION: Readonly<XrunSessionCounter> = Object.freeze({
	xruns: 0,
	parked: 0,
	reports: 0,
	callbacks: 0,
	worst_gap_ms: 0
});

function _assertPositive(name: string, value: number): void {
	if (!Number.isFinite(value) || value <= 0) {
		throw new RangeError(`${name} must be a finite positive number, got ${value}`);
	}
}

export function quantumDurationMs(
	quantumFrames: number,
	sampleRateHz: number
): number {
	_assertPositive('quantumFrames', quantumFrames);
	_assertPositive('sampleRateHz', sampleRateHz);
	return (quantumFrames / sampleRateHz) * 1000;
}

/**
 * The inter-callback gap above which the audio thread is judged late.
 *
 * `deviceBufferMs` is `AudioContext.baseLatency * 1000`. It can read 0 on a
 * context that has not been given a device yet, which is why the quantum is the
 * floor: a threshold of zero would report every callback as an xrun and the
 * counter would be pure noise from the first second.
 */
export function xrunGapThresholdMs(
	quantumMs: number,
	deviceBufferMs: number,
	factor: number = XRUN_GAP_FACTOR,
	floorMs: number = XRUN_GAP_FLOOR_MS
): number {
	_assertPositive('quantumMs', quantumMs);
	if (!Number.isFinite(deviceBufferMs) || deviceBufferMs < 0) {
		throw new RangeError(`deviceBufferMs must be finite and non-negative, got ${deviceBufferMs}`);
	}
	if (!Number.isFinite(factor) || factor <= 1) {
		throw new RangeError(
			`factor must exceed 1, got ${factor}: a threshold at or below the buffer period ` +
				'reports the normal burst-render rhythm as a dropout'
		);
	}
	if (!Number.isFinite(floorMs) || floorMs < 0) {
		throw new RangeError(`floorMs must be finite and non-negative, got ${floorMs}`);
	}
	return Math.max(quantumMs, deviceBufferMs) * factor + floorMs;
}

/**
 * The rule, in one comparison. `src/lib/rb/xrun-sentinel-processor.js` inlines
 * the same two comparisons because a worklet scope cannot import; the unit test
 * pins both texts against each other so they cannot drift apart.
 */
export function classifyGapMs(
	gapMs: number,
	thresholdMs: number,
	parkedGapMs: number = XRUN_PARKED_GAP_MS
): XrunGapKind {
	if (!Number.isFinite(gapMs) || gapMs < 0) {
		throw new RangeError(`gapMs must be finite and non-negative, got ${gapMs}`);
	}
	_assertPositive('thresholdMs', thresholdMs);
	_assertPositive('parkedGapMs', parkedGapMs);
	if (gapMs >= parkedGapMs) return 'parked';
	if (gapMs > thresholdMs) return 'xrun';
	return 'ok';
}

export function foldXrunGap(
	tally: Readonly<XrunTally>,
	gapMs: number,
	thresholdMs: number,
	parkedGapMs: number = XRUN_PARKED_GAP_MS
): XrunTally {
	const kind = classifyGapMs(gapMs, thresholdMs, parkedGapMs);
	return {
		callbacks: tally.callbacks + 1,
		xruns: tally.xruns + (kind === 'xrun' ? 1 : 0),
		parked: tally.parked + (kind === 'parked' ? 1 : 0),
		// A parked gap is not a dropout, so it must not become the worst one -
		// a single hidden tab would otherwise pin worst_gap_ms at 40s forever.
		worstGapMs: kind === 'xrun' ? Math.max(tally.worstGapMs, gapMs) : tally.worstGapMs
	};
}

/** Port messages are untrusted structured clones: validate before folding. */
export function isXrunReport(value: unknown): value is XrunReport {
	if (typeof value !== 'object' || value === null) return false;
	const row = value as Record<string, unknown>;
	for (const key of ['xruns', 'parked', 'callbacks', 'worst_gap_ms', 'window_ms', 'threshold_ms']) {
		const field = row[key];
		if (typeof field !== 'number' || !Number.isFinite(field) || field < 0) return false;
	}
	return typeof row.clock === 'string' && row.clock.length > 0;
}

export function foldXrunReport(
	counter: Readonly<XrunSessionCounter>,
	report: XrunReport
): XrunSessionCounter {
	return {
		xruns: counter.xruns + report.xruns,
		parked: counter.parked + report.parked,
		reports: counter.reports + 1,
		callbacks: counter.callbacks + report.callbacks,
		worst_gap_ms: Math.max(counter.worst_gap_ms, report.worst_gap_ms)
	};
}

/**
 * The perf-ring line for one report.
 *
 * Denominator honesty (house rule): the callback count and the window travel
 * with the xrun count, so a reader can say "3 xruns in 2000ms over 740
 * callbacks" instead of quoting a bare 3 against nothing.
 */
export function xrunReportMessage(report: XrunReport): string {
	const round = (value: number): number => Math.round(value * 1000) / 1000;
	return (
		`${report.xruns} xrun(s) in ${round(report.window_ms)}ms over ${report.callbacks} ` +
		`audio callbacks; worst gap ${round(report.worst_gap_ms)}ms against a ` +
		`${round(report.threshold_ms)}ms late-callback threshold ` +
		`(${report.parked} parked gap(s) excluded, clock=${report.clock})`
	);
}
