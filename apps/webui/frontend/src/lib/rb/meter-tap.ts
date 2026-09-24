/**
 * Level meter taps: an AudioWorklet that measures one point in the graph.
 *
 * DECK-AGNOSTIC ON PURPOSE. A tap is a tap on an AudioNode; the caller owns
 * which deck (or bus) it belongs to. That keeps this module off deck-slots,
 * which sits at the fan-in allowance, and it is what lets a master meter
 * reuse this later without inventing a deck id it does not have.
 *
 * The channel taps are connected post-fader.
 *
 * WHERE IT TAPS, AND WHY THAT IS THE WHOLE FEATURE. The tap is connected from
 * `fader`, which is post-trim, post-EQ, and post-channel-fader. The channel
 * meter sits on the fader itself, so its volume slider and indicator always
 * describe the same audible channel level.
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

/**
 * Wall clock for meter ballistics. Falls back to Date.now() only where
 * performance.now() is genuinely absent, and both are monotonic enough for a
 * decay measured in hundreds of milliseconds.
 *
 * Lives here, beside `readMeterTap`'s `nowMs` parameter, because the reason
 * that parameter exists at all is so the ballistics are testable without
 * faking a clock: the default clock and the function that consumes it belong
 * in one module rather than one owning the policy and another the time.
 */
export function meterClockMs(): number {
	return typeof performance === 'object' && typeof performance.now === 'function'
		? performance.now()
		: Date.now();
}

/**
 * The reading every meter falls back to when its tap does not exist yet:
 * floored, unlit, unclipped. Shared rather than written out at each call site
 * so "no graph yet" can never come to mean two slightly different things.
 */
export const SILENT_METER_READING: Readonly<MeterReading> = Object.freeze({
	db: METER_FLOOR_DBFS,
	peakDb: METER_FLOOR_DBFS,
	segments: 0,
	normalized: 0,
	clipped: false
});

/**
 * True once `armDeckMeters`'s `attachMeterTaps` has failed for the CURRENT
 * context. AGENTS.md L244-L246 forbids masking a terminal processor error,
 * and returning `SILENT_METER_READING` when the worklet never armed does
 * exactly that: a broken meter renders identically to a genuinely silent
 * master bus forever, with nothing but a perf-event row (which nobody is
 * watching live) telling the two apart.
 *
 * Deliberately module-level rather than per-tap: `attachMeterTaps` fails or
 * succeeds ONCE, for every source in that call's `meterSources` array (they
 * all share the one `ctx.audioWorklet.addModule` and the one
 * `AudioWorkletNode` constructor that can reject), so one flag for "did this
 * context's meters arm" is the honest granularity - not a per-tap guess.
 */
let _metersUnavailable = false;

/**
 * The context whose meters are currently armed (or being armed).
 *
 * `armDeckMeters` is fire-and-forget and route cleanup calls `dispose()`
 * without awaiting it, so a rejection from a context that has since been
 * torn down can arrive AFTER a remount has armed a healthy new one. Without
 * this, that stale rejection marks the new session's meters permanently
 * unavailable and a working meter renders as broken forever. Scoping the
 * verdict to the context that produced it is the fix: a failure only counts
 * for the graph it happened in.
 */
let _armingContext: AudioContext | null = null;

/**
 * Set by `armDeckMeters`'s catch. See `_metersUnavailable` above.
 *
 * Returns whether the verdict was ACCEPTED: a failure from a context that is
 * no longer the armed one is dropped, because it says nothing about the graph
 * now on screen. The boolean is returned rather than swallowed so the caller
 * can record which of the two actually happened.
 */
export function markMetersUnavailable(ctx: AudioContext): boolean {
	if (ctx !== _armingContext) return false;
	_setMetersUnavailable(true);
	return true;
}

/**
 * Listeners for a change in the unavailable verdict.
 *
 * A meter component cannot POLL this: its poll is the RAF loop, and that loop
 * is gated on something playing precisely so an idle session costs no
 * main-thread work. But arming happens on graph build, which can be while
 * nothing is playing at all, so a terminal worklet failure would otherwise
 * become visible only once a deck started - exactly the "a broken meter reads
 * as a silent one" masking AGENTS.md L244-L246 forbids. A notification costs
 * nothing while idle and arrives whenever the verdict actually changes.
 */
const _unavailableListeners = new Set<(unavailable: boolean) => void>();

function _setMetersUnavailable(next: boolean): void {
	if (_metersUnavailable === next) return;
	_metersUnavailable = next;
	for (const listener of _unavailableListeners) listener(next);
}

/**
 * Subscribe to CHANGES in the unavailable verdict; returns an unsubscribe.
 *
 * Deliberately does not fire on subscription: the current value is what
 * {@link metersUnavailable} is for, and a subscriber that needs both reads one
 * and listens for the other rather than depending on a first-call convention.
 */
export function onMetersUnavailableChange(
	listener: (unavailable: boolean) => void
): () => void {
	_unavailableListeners.add(listener);
	return () => {
		_unavailableListeners.delete(listener);
	};
}

/** Read by a meter component (or its reading path) to render an honest
 * unavailable state instead of a fabricated silent one. */
export function metersUnavailable(): boolean {
	return _metersUnavailable;
}

/**
 * The reading a meter reports when arming genuinely failed, as opposed to
 * "no graph built yet" ({@link SILENT_METER_READING}). Same floored, unlit,
 * unclipped SHAPE (so a numeric consumer needs no new branch), but a
 * DIFFERENT frozen object identity so a caller that needs to tell the two
 * apart - the UI - can, via {@link metersUnavailable}.
 */
export const UNAVAILABLE_METER_READING: Readonly<MeterReading> = Object.freeze({
	db: METER_FLOOR_DBFS,
	peakDb: METER_FLOOR_DBFS,
	segments: 0,
	normalized: 0,
	clipped: false
});

/**
 * The master bus's tap. It lives HERE rather than in audio-engine for the
 * reason audio-context-instrumentation.ts already states for the other
 * instruments (convention D5): the engine keeps the call sites, and the
 * measurement keeps its own module.
 *
 * It is a SEPARATE observer from the engine's `_masterAnalyser` (the silence
 * watchdog): that one answers "is anything playing at all", this one answers
 * "what level is leaving the master bus", and it rides the same
 * meter-tap/meter-math pathway the deck taps use, so a master meter and a
 * channel meter can never disagree about what a colour band means.
 */
let _masterTap: MeterTap | null = null;

/**
 * Create the master tap and return its `meterSources` entry, so the engine's
 * graph build hands it to the ONE existing `attachMeterTaps` call rather than
 * opening a second pathway.
 *
 * `gain` is typed as `AudioNode` rather than `GainNode` so a test can pass a
 * plain stub with no real Web Audio graph behind it, and it is returned
 * rather than connected here because attaching is the caller's async step.
 */
export function createMasterMeterSource(gain: AudioNode): MeterTapSource {
	_masterTap = createMeterTap();
	return { tap: _masterTap, source: gain };
}

/**
 * Master output level, taken POST MASTER GAIN through the same meter-math
 * policy the per-channel meters use.
 *
 * Deliberately distinct from a channel reading: the channel taps sit post-trim,
 * post-EQ, and post-channel-fader (issue #3529), so they track each deck's
 * channel fader rather than the master bus. This one taps downstream of
 * `_masterGain`, which is what pin 5a5c3b8033d8 asked for.
 *
 * NOT a speaker-damage reading: everything downstream of the master bus (OS
 * volume, the audio interface, the amplifier, its limiter) is invisible from
 * here, and a deck on external USB routing bypasses the master bus entirely.
 * See docs/research/adrian-level-meters-clipping-lights.md.
 */
export function masterMeterReading(nowMs: number): MeterReading {
	if (_metersUnavailable) return UNAVAILABLE_METER_READING;
	if (_masterTap === null) return SILENT_METER_READING;
	return readMeterTap(_masterTap, nowMs);
}

/**
 * The live master tap's worklet node, or null when no graph is armed.
 *
 * Exists for the Chromium suite, which has to reach the REAL node to prove the
 * `onprocessorerror` wiring: a `processorerror` cannot be provoked from
 * JavaScript (only the browser raises one, when the processor's own
 * constructor or `process()` throws), so the test dispatches the real event at
 * the real handler this module installed. A test that rebuilt its own node
 * would prove nothing about the shipped one, which is the failure
 * `master-meter-browser-entry.ts` was written to avoid.
 */
export function masterMeterNode(): AudioWorkletNode | null {
	return _masterTap === null ? null : _masterTap.node;
}

/**
 * Drop the master tap reference with the graph that carried it. The tap's own
 * node is already released by `teardownMeterTaps`; this keeps the
 * null-means-no-graph contract exact rather than leaving a dead tap readable.
 */
export function releaseMasterMeterTap(): void {
	_masterTap = null;
}

export async function attachMeterTaps(
	ctx: AudioContext,
	sources: ReadonlyArray<MeterTapSource>
): Promise<void> {
	// Optimistic: this attempt may succeed even if a previous context's did
	// not. armDeckMeters's catch sets the flag back to true if THIS attempt
	// also fails, so a stale unavailable state never survives a working retry.
	_setMetersUnavailable(false);
	// Claim ownership of the verdict BEFORE the first await, so a rejection
	// raised anywhere below is attributable to this context and a later
	// context's arm supersedes it. See `_armingContext`.
	_armingContext = ctx;
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
		// A processor that crashes AFTER arming leaves this node permanently
		// silent, and the arming promise has already RESOLVED by then, so its
		// catch can never see it. Without this the reading decays to an
		// ordinary silent state and a dead meter renders exactly like a quiet
		// master bus - the masking AGENTS.md L244-L246 forbids and the whole
		// reason _metersUnavailable exists. Routed through
		// markMetersUnavailable so the verdict stays scoped to the context
		// that produced it: a crash in a torn-down graph must not condemn the
		// one now on screen.
		node.onprocessorerror = (): void => {
			markMetersUnavailable(ctx);
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
			tap.node.onprocessorerror = null;
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
	// A torn-down graph has no meters at all, which is SILENT_METER_READING's
	// case, not "this context's arming failed" - reset so the next graph
	// starts from a clean, unproven-broken state.
	_setMetersUnavailable(false);
	// And nothing is armed any more, so a late rejection from the context just
	// dropped has no owner and is ignored rather than blamed on the next graph.
	_armingContext = null;
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
