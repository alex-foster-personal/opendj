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
 * `tests/unit/xrun-sentinel.test.mjs` pins the two texts against each other,
 * and `tests/unit/xrun-sentinel-cadence.test.mjs` pins the cadence quantile the
 * same way.
 *
 * IT MEASURES ITS OWN CADENCE, and that is the whole reason this file changed
 * on Wed 2 Sep 2026. The threshold used to arrive from the main thread, derived
 * from `AudioContext.baseLatency` - the only period the main thread can see.
 * On a real device baseLatency under-reported a 512-frame HAL buffer as roughly
 * one 128-frame quantum, and the resulting 5.354ms threshold called every
 * FOURTH callback of a perfectly healthy machine a dropout: 173 xruns in 692
 * callbacks, every one of them false. The audio thread is the only place the
 * true callback period is observable, so it is measured here. The main thread
 * still owns every POLICY number (the quantile, the window, the factor, the
 * warmup length); what it no longer does is guess the device.
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
		this.cadenceQuantile = settings.cadenceQuantile;
		this.cadenceWindow = settings.cadenceWindow;
		this.warmupCallbacks = settings.warmupCallbacks;
		this.gapFactor = settings.gapFactor;
		this.gapFloorMs = settings.gapFloorMs;
		if (
			!Number.isFinite(this.thresholdMs) ||
			!Number.isFinite(this.parkedGapMs) ||
			!Number.isFinite(this.reportIntervalMs) ||
			!Number.isFinite(this.cadenceQuantile) ||
			!Number.isFinite(this.cadenceWindow) ||
			!Number.isFinite(this.warmupCallbacks) ||
			!Number.isFinite(this.gapFactor) ||
			!Number.isFinite(this.gapFloorMs)
		) {
			// Fail at construction rather than silently counting against NaN,
			// which would classify every callback as healthy forever.
			throw new RangeError(
				'xrun sentinel requires finite thresholdMs, parkedGapMs, reportIntervalMs, ' +
					'cadenceQuantile, cadenceWindow, warmupCallbacks, gapFactor and gapFloorMs ' +
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
		// Ring of recent inter-callback gaps, and how many of it are real. The
		// cadence estimate is drawn from these and from nothing else.
		this.gaps = new Float64Array(this.cadenceWindow);
		this.gapAt = 0;
		this.gapCount = 0;
		// Until the cadence is measured, this counts callbacks and judges NOTHING.
		// The incoming thresholdMs is a seed for the first report only; no gap is
		// ever classified against it, because it is the number that was wrong.
		this.judging = false;
		this.callbacks = 0;
		this.xruns = 0;
		this.parked = 0;
		this.worstGapMs = 0;
		this.flushRequestIds = [];
		this.port.onmessage = (event) => {
			const data = event.data;
			if (data?.kind === 'xrun-flush' && typeof data.requestId === 'string') {
				this.flushRequestIds.push(data.requestId);
			}
		};
	}

	/**
	 * Re-measure the device's callback period from the ring, and re-derive the
	 * threshold from it.
	 *
	 * A QUANTILE, not the maximum: the maximum IS the dropout, so taking it would
	 * let one genuine 30ms stall redefine the cadence as 30ms and hide every
	 * stall after it. Parked gaps are dropped first - one hidden tab would
	 * otherwise push the quantile past every real dropout.
	 *
	 * Called once per report window rather than per callback: this sorts, and a
	 * sort at ~350Hz on the audio thread would make the instrument the load it is
	 * measuring.
	 */
	remeasureCadence() {
		const usable = [];
		for (let i = 0; i < this.gapCount; i += 1) {
			const gapMs = this.gaps[i];
			if (gapMs >= 0 && gapMs < this.parkedGapMs) usable.push(gapMs);
		}
		if (usable.length === 0) return;
		usable.sort((a, b) => a - b);
		const at = Math.min(
			usable.length - 1,
			Math.floor(this.cadenceQuantile * (usable.length - 1))
		);
		const periodMs = usable[at];
		if (!(periodMs > 0)) return;
		this.thresholdMs = periodMs * this.gapFactor + this.gapFloorMs;
		this.judging = true;
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
		this.gaps[this.gapAt] = gapMs;
		this.gapAt = (this.gapAt + 1) % this.cadenceWindow;
		if (this.gapCount < this.cadenceWindow) this.gapCount += 1;
		if (!this.judging && this.gapCount >= this.warmupCallbacks) this.remeasureCadence();
		// Silence during warmup is deliberate and is NOT a blind spot being
		// tolerated: a threshold nobody has measured yet can only produce noise,
		// and the alternative is exactly the 1-in-4 false-positive rate this
		// replaces. Roughly 1.5s on a 512-frame device.
		if (this.judging) {
			if (gapMs >= this.parkedGapMs) {
				this.parked += 1;
			} else if (gapMs > this.thresholdMs) {
				this.xruns += 1;
				if (gapMs > this.worstGapMs) this.worstGapMs = gapMs;
			}
		}
		const windowMs = nowMs - this.windowStartMs;
		const flushRequestIds = this.flushRequestIds.splice(0);
		if (windowMs < this.reportIntervalMs && flushRequestIds.length === 0) return true;
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
		if (this.judging) this.remeasureCadence();
		this.windowStartMs = nowMs;
		this.callbacks = 0;
		this.xruns = 0;
		this.parked = 0;
		this.worstGapMs = 0;
		for (const requestId of flushRequestIds) {
			this.port.postMessage({ kind: 'xrun-flush-ack', requestId, judging: this.judging });
		}
		return true;
	}
}

registerProcessor('mdt-xrun-sentinel', XrunSentinelProcessor);
