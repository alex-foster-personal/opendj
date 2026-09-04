/**
 * Level meter taps: an AudioWorklet that measures one point in the graph.
 *
 * DECK-AGNOSTIC ON PURPOSE. A tap is a tap on an AudioNode; the caller owns
 * which deck (or bus) it belongs to. That keeps this module off deck-slots,
 * which sits at the fan-in allowance, and it is what lets a master meter
 * reuse this later without inventing a deck id it does not have.
 *
 * The channel taps are connected post-EQ, pre-fader.
 *
 * WHERE IT TAPS, AND WHY THAT IS THE WHOLE FEATURE. The tap is connected from
 * `high`, which is post-trim and post-EQ but pre-fader. That is the DJM
 * convention, and it is the point: you set trim until the meter reads right,
 * and the fader is then free for mixing without the meter lying to you.
 *
 * The AnalyserNode this replaces was connected BEFORE `trim` (it still is, for
 * `captureDeckAudio`, which wants raw deck output). A pre-trim tap sees neither
 * the trim knob nor the EQ, so it could only ever report how loud the FILE is,
 * which is a number that could have been precomputed offline. It also could not
 * show clipping, because clipping happens downstream at the master bus.
 *
 * Anything upstream of the tap is included for free, forever, including effects
 * that do not exist yet. That is why a tap beats predicting the level by
 * summing gain contributions: a feedback delay accumulates energy over seconds
 * and a resonant filter adds gain at its cutoff, and a summed prediction would
 * report a flat line while the real output climbed into the red.
 *
 * Background: docs/research/adrian-level-meters-clipping-lights.md
 */

import {
	INITIAL_PEAK_HOLD,
	METER_FLOOR_DBFS,
	dbfsFromAmplitude,
	isClipping,
	normalizedFromDbfs,
	segmentsLitFromDbfs,
	stepBallistics,
	stepPeakHold,
	type PeakHoldState
} from '$lib/rb/meter-math';

import meterProcessorModuleUrl from '$lib/rb/meter-processor.js?url';

// Re-exported so a consumer needs one import edge into the metering pair, not
// two. The metric that notices is frontend.max_fan_out.
export { METER_FLOOR_DBFS } from '$lib/rb/meter-math';

const CHANNEL_METER_PROCESSOR_NAME = 'mdt-channel-meter';

/**
 * How often the worklet posts. 20ms is denser than a 60Hz frame, so the UI
 * almost always has a fresh observation, and the peak is folded across every
 * quantum in between so nothing is missed regardless.
 */
const METER_REPORT_INTERVAL_S = 0.02;

/** Silent by construction, and silenced again by a zero gain on the way out. */
const METER_NODE_OPTIONS: Readonly<AudioWorkletNodeOptions> = Object.freeze({
	numberOfInputs: 1,
	numberOfOutputs: 1,
	outputChannelCount: [1]
});

interface _Observation {
	seq: number;
	peak: number;
}

interface _Ballistic {
	db: number;
	peak: PeakHoldState;
	consumedSeq: number;
	lastReadMs: number;
	clipLatchedUntilMs: number;
}

/** A tap handle. Created synchronously so a caller can store it during a
 * synchronous graph build, then attached once the worklet module has loaded. */
export interface MeterTap {
	latest: _Observation | null;
	ballistic: _Ballistic;
	node: AudioWorkletNode | null;
}

/** One tap and the node it reads. */
export interface MeterTapSource {
	tap: MeterTap;
	source: AudioNode;
}

export interface MeterReading {
	/** Ballistic level, dBFS. Floors at METER_FLOOR_DBFS, never -Infinity. */
	db: number;
	/** Held peak marker, dBFS. */
	peakDb: number;
	/** Lit segment count, 0..10. */
	segments: number;
	/** Position on the displayed scale, 0..1. */
	normalized: number;
	/** True while the clip warning is latched. */
	clipped: boolean;
}

/** How long a clip stays lit after the level that caused it has passed. A
 * single-frame red flash is invisible; this makes it readable without lying
 * about the current level. */
const CLIP_LATCH_MS = 1200;

function _emptyBallistic(): _Ballistic {
	return {
		db: METER_FLOOR_DBFS,
		peak: INITIAL_PEAK_HOLD,
		consumedSeq: 0,
		lastReadMs: 0,
		clipLatchedUntilMs: 0
	};
}

let _taps: MeterTap[] = [];
let _sink: GainNode | null = null;

// --------------------------------------------------------------------------
// install
// --------------------------------------------------------------------------

function _isObservation(data: unknown): data is _Observation {
	if (typeof data !== 'object' || data === null) return false;
	const candidate = data as Partial<_Observation>;
	return Number.isFinite(candidate.seq) && Number.isFinite(candidate.peak);
}

/**
 * Load the meter module into `ctx` and attach one tap per supplied source.
 *
 * Rejects rather than reporting its own failure, so the single caller owns the
 * one catch and the failure is recorded in exactly one place.
 */
export function createMeterTap(): MeterTap {
	return { latest: null, ballistic: _emptyBallistic(), node: null };
}

export async function attachMeterTaps(
	ctx: AudioContext,
	sources: ReadonlyArray<MeterTapSource>
): Promise<void> {
	if (ctx.audioWorklet === undefined) {
		throw new Error('AudioWorklet is unavailable; channel level meters cannot start');
	}
	await ctx.audioWorklet.addModule(meterProcessorModuleUrl);
	const sink = ctx.createGain();
	sink.gain.value = 0;
	sink.connect(ctx.destination);
	_sink = sink;
	for (const { tap, source } of sources) {
		const node = new AudioWorkletNode(ctx, CHANNEL_METER_PROCESSOR_NAME, {
			...METER_NODE_OPTIONS,
			processorOptions: { reportIntervalS: METER_REPORT_INTERVAL_S }
		});
		node.port.onmessage = (event: MessageEvent): void => {
			if (!_isObservation(event.data)) {
				throw new TypeError(
					`a channel meter posted an unreadable message: ${JSON.stringify(event.data)}`
				);
			}
			tap.latest = event.data;
		};
		source.connect(node);
		node.connect(sink);
		tap.node = node;
		_taps.push(tap);
	}
}

/** Drop every tap with the graph that carried it. */
export function teardownMeterTaps(): void {
	for (const tap of _taps) {
		if (tap.node !== null) {
			tap.node.port.onmessage = null;
			tap.node.disconnect();
			tap.node = null;
		}
		tap.latest = null;
		tap.ballistic = _emptyBallistic();
	}
	_taps = [];
	if (_sink !== null) {
		_sink.disconnect();
		_sink = null;
	}
}

// --------------------------------------------------------------------------
// read
// --------------------------------------------------------------------------

/**
 * Current reading for one deck, with PPM ballistics applied.
 *
 * `nowMs` is a parameter rather than a `performance.now()` call so the
 * ballistics are testable without faking a clock.
 */
export function readMeterTap(tap: MeterTap, nowMs: number): MeterReading {
	if (!Number.isFinite(nowMs)) {
		throw new RangeError(`readMeterTap: nowMs must be finite, got ${nowMs}`);
	}
	const state = tap.ballistic;
	const observation = tap.latest;
	// First read of a session has no elapsed time to decay over.
	const elapsedS = state.lastReadMs === 0 ? 0 : Math.max(0, (nowMs - state.lastReadMs) / 1000);
	state.lastReadMs = nowMs;

	// Attack ONLY on a new observation. Re-attacking to a stale peak on every
	// frame between posts would pin the meter and it would never fall.
	let observedDb = METER_FLOOR_DBFS;
	if (observation !== null && observation.seq !== state.consumedSeq) {
		state.consumedSeq = observation.seq;
		observedDb = Math.max(METER_FLOOR_DBFS, dbfsFromAmplitude(observation.peak));
		if (isClipping(observedDb)) state.clipLatchedUntilMs = nowMs + CLIP_LATCH_MS;
	}

	state.db = stepBallistics(state.db, observedDb, elapsedS);
	state.peak = stepPeakHold(state.peak, observedDb, elapsedS);
	return {
		db: state.db,
		peakDb: state.peak.db,
		segments: segmentsLitFromDbfs(state.db),
		normalized: normalizedFromDbfs(state.db),
		clipped: nowMs < state.clipLatchedUntilMs
	};
}
