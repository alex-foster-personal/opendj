/**
 * S1 / Q2: the audio-thread glitch detector, as an AudioWorkletProcessor.
 *
 * Runs inside AudioWorkletGlobalScope, so this file is deliberately plain: no
 * imports (a worklet scope cannot resolve them), no TypeScript, no dependency
 * on anything the bundler would have to rewrite. Its ONLY job is to time its
 * own callbacks and post a small tally; every threshold is computed on the main
 * thread by `$lib/rb/xrun-math` and handed in through `processorOptions`, so
 * this file holds no policy of its own.
 *
 * The two comparisons below are the same rule as `classifyGapMs` in
 * `xrun-math.ts`, written out because a worklet cannot import it.
 * `tests/unit/xrun-sentinel.test.mjs` pins the two texts against each other.
 *
 * It NEVER writes audio: `process()` fills nothing and returns true, so the
 * single mono output stays silent and the node exists purely to be scheduled.
 * The main thread connects it through a zero gain, so even a bug here cannot
 * put a sample into the master bus.
 */

/* global AudioWorkletProcessor, registerProcessor, currentTime */

class XrunSentinelProcessor extends AudioWorkletProcessor {
	constructor(options) {
		super();
		const settings = (options && options.processorOptions) || {};
		this.thresholdMs = settings.thresholdMs;
		this.parkedGapMs = settings.parkedGapMs;
		this.reportIntervalMs = settings.reportIntervalMs;
		if (
			!Number.isFinite(this.thresholdMs) ||
			!Number.isFinite(this.parkedGapMs) ||
			!Number.isFinite(this.reportIntervalMs)
		) {
			// Fail at construction rather than silently counting against NaN,
			// which would classify every callback as healthy forever.
			throw new RangeError(
				'xrun sentinel requires finite thresholdMs, parkedGapMs and reportIntervalMs ' +
					'in processorOptions'
			);
		}
		// performance.now() is NOT available in an AudioWorkletGlobalScope.
		// Measured Sun 31 Aug 2026 against a real browser and the built asset:
		// every report came back clock="date", i.e. this is the branch that
		// actually runs, not a defensive one. Date.now() is 1ms-coarse, which
		// is well inside a threshold of ~9.7ms, and only deltas matter here.
		// The probe stays because a scope that does expose it should use it,
		// and the answer travels with every report rather than being assumed.
		const hasPerformance =
			typeof performance === 'object' && performance !== null &&
			typeof performance.now === 'function';
		this.clock = hasPerformance ? 'performance' : 'date';
		this.now = hasPerformance ? () => performance.now() : () => Date.now();
		this.lastCallbackMs = null;
		this.windowStartMs = null;
		this.callbacks = 0;
		this.xruns = 0;
		this.parked = 0;
		this.worstGapMs = 0;
	}

	process() {
		const nowMs = this.now();
		if (this.lastCallbackMs === null) {
			this.lastCallbackMs = nowMs;
			this.windowStartMs = nowMs;
			return true;
		}
		const gapMs = nowMs - this.lastCallbackMs;
		this.lastCallbackMs = nowMs;
		this.callbacks += 1;
		if (gapMs >= this.parkedGapMs) {
			this.parked += 1;
		} else if (gapMs > this.thresholdMs) {
			this.xruns += 1;
			if (gapMs > this.worstGapMs) this.worstGapMs = gapMs;
		}
		const windowMs = nowMs - this.windowStartMs;
		if (windowMs < this.reportIntervalMs) return true;
		// Silence is the healthy case and it must cost nothing: a window with
		// nothing in it rolls over without posting, so an idle set produces no
		// MessagePort traffic and no ring rows at all.
		if (this.xruns > 0 || this.parked > 0) {
			this.port.postMessage({
				xruns: this.xruns,
				parked: this.parked,
				callbacks: this.callbacks,
				worst_gap_ms: this.worstGapMs,
				window_ms: windowMs,
				threshold_ms: this.thresholdMs,
				clock: this.clock
			});
		}
		this.windowStartMs = nowMs;
		this.callbacks = 0;
		this.xruns = 0;
		this.parked = 0;
		this.worstGapMs = 0;
		return true;
	}
}

registerProcessor('mdt-xrun-sentinel', XrunSentinelProcessor);
