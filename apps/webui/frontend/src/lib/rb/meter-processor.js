/**
 * Channel level meter tap, as an AudioWorkletProcessor.
 *
 * Runs inside AudioWorkletGlobalScope, so this file is deliberately plain: no
 * imports (a worklet scope cannot resolve them), no TypeScript. It MEASURES and
 * decides nothing. Every threshold, ballistic rate and color band lives in
 * `$lib/rb/meter-math`, on the main thread, where it is unit tested without an
 * AudioContext. Same split as `xrun-sentinel-processor`.
 *
 * WHY THE PEAK IS ACCUMULATED RATHER THAN SAMPLED: the main thread reads at
 * frame rate, which is far slower than the audio callback. Reporting only the
 * level at post time would miss every transient between posts, and transients
 * are the entire reason a peak meter exists. So `process()` folds the maximum
 * across every quantum in the window, and the post carries that maximum. No
 * sample is unobserved.
 *
 * It NEVER writes audio: `process()` fills nothing, so the single mono output
 * stays silent, and the main thread connects it through a zero gain as well.
 * A bug here cannot put a sample into the master bus.
 *
 * It reports PEAK ONLY. An RMS was computed here per sample and posted
 * alongside it until Fri 4 Sep 2026; nothing ever read it, so it was a
 * multiply-add per sample per deck on the AUDIO THREAD producing a number
 * with no consumer. Re-add it only with a reader.
 *
 * Each post carries a monotonic `seq`. The main thread applies the meter's
 * instantaneous attack ONLY when it sees a new `seq`, and decays otherwise.
 * Without that, a stale peak would be re-attacked on every frame between posts
 * and the meter would never fall.
 */

/* global AudioWorkletProcessor, registerProcessor, sampleRate */

class MeterProcessor extends AudioWorkletProcessor {
	constructor(options) {
		super();
		const settings = (options && options.processorOptions) || {};
		this.reportIntervalS = settings.reportIntervalS;
		if (!Number.isFinite(this.reportIntervalS) || this.reportIntervalS <= 0) {
			// Fail at construction rather than posting against NaN, which would
			// either flood the port or never post at all.
			throw new RangeError(
				`meter tap requires a finite positive reportIntervalS in processorOptions, got ${this.reportIntervalS}`
			);
		}
		this.windowPeak = 0;
		this.windowElapsedS = 0;
		this.seq = 0;
		// Whether the last posted window carried signal, so exactly ONE post
		// marks the transition into silence and none follow it.
		this.postedSignal = false;
	}

	process(inputs) {
		const input = inputs[0];
		// No connected channels yet, or a deck with nothing loaded. Report
		// silence on the normal cadence rather than going quiet, so the main
		// thread can tell "silent" from "tap is dead".
		const channels = input === undefined ? [] : input;
		let frames = 0;
		for (let c = 0; c < channels.length; c += 1) {
			const samples = channels[c];
			if (samples === undefined) continue;
			frames = samples.length;
			for (let i = 0; i < samples.length; i += 1) {
				const value = samples[i];
				const magnitude = value < 0 ? -value : value;
				if (magnitude > this.windowPeak) this.windowPeak = magnitude;
			}
		}
		// A quantum with no channels still advances time, otherwise a silent
		// deck would never reach its report interval and the last live reading
		// would sit on screen forever.
		if (frames === 0) frames = 128;
		this.windowElapsedS += frames / sampleRate;
		if (this.windowElapsedS < this.reportIntervalS) return true;

		// SILENCE COSTS NOTHING, the same rule xrun-sentinel-processor follows.
		// A window with no signal rolls over without posting, so four taps on a
		// stopped set produce no MessagePort traffic at all. Measured before this
		// guard: 197 messages a second, forever, from four silent decks.
		//
		// Safe because the reader already decays toward the floor when no new
		// `seq` arrives, so no post and a silent post are behaviourally identical
		// on the main thread. One post still marks the transition INTO silence,
		// so a lit meter starts falling from the level it actually reached.
		const hadSignal = this.windowPeak > 0;
		if (hadSignal || this.postedSignal) {
			this.seq += 1;
			this.port.postMessage({ seq: this.seq, peak: this.windowPeak });
		}
		this.postedSignal = hadSignal;
		this.windowPeak = 0;
		this.windowElapsedS = 0;
		return true;
	}
}

registerProcessor('mdt-channel-meter', MeterProcessor);
