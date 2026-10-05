/**
 * One playhead clock per deck: dead reckoning with drift reconciliation
 * (ANIM-CLOCK-01, Mon 5 Oct 2026).
 *
 * the maintainer: "animations (waveform + rotating beat thingies etc) should probably be
 * smoothed via assumption with jump-catch up if it desyncs".
 *
 * The engine publishes a deck's position once per tick, and that sample moves
 * in steps the screen does not: the output clock advances in device-buffer
 * chunks, repeats a value for a frame or two, and the frame that notices it can
 * be early or late. Painting the raw sample shows all of that as jitter. This is
 * the standard DJ-software answer:
 *
 * - DEAD RECKONING. Each authoritative sample is an anchor: a position, the
 *   instant it was true on the audio clock (`AudioContext.getOutputTimestamp()`'s
 *   `performanceTime`, never the frame that noticed it), and the tempo. Every
 *   frame projects `position + (now - sampledAt) x tempo`.
 * - RECONCILIATION. When the next sample arrives, the gap between what is on
 *   screen and the new projection is the drift. A small gap is slewed out over
 *   a few frames, so nothing visibly jumps; a big one, or a real discontinuity
 *   (load, play/stop, a tempo step, a seek or loop jump, which all show up as a
 *   big gap), snaps at once. Every sample re-anchors, so a tempo change never
 *   accumulates error.
 * - NEVER PAST WHAT WAS HEARD. Paused, or the presentation clock is untrusted
 *   (stalled output, starved worklet): the display freezes on the last
 *   confirmed sample. Projection is also capped at `maxProjectionMs` past the
 *   sample, so a clock that silently stops feeding samples freezes rather than
 *   running on.
 *
 * Pure: no Svelte, no DOM, no globals. `playhead-display.svelte.ts` owns one
 * state per deck and is what consumers read.
 */

/** Every threshold in one place. Proposed values, to be calibrated against the maintainer's eye. */
export const PLAYHEAD_CLOCK_CONFIG = {
	/** A drift at or under this is slewed out over `slewWindowMs`. */
	slewMaxErrorMs: 15,
	/** About four frames at 60 fps: a 15 ms drift moves the playhead 3.75 ms/frame off tempo for four frames. */
	slewWindowMs: 67,
	/** A drift between `slewMaxErrorMs` and `snapErrorMs` is caught up faster, over about two frames. */
	catchUpWindowMs: 33,
	/** A drift over this is not drift, it is a discontinuity: snap. */
	snapErrorMs: 50,
	/** A tempo change of this ratio or more between samples is a step: snap. Smaller ones (sync nudges) re-anchor and slew. */
	tempoStepRatio: 0.01,
	/** Never project further than this past (or before) the last confirmed sample. */
	maxProjectionMs: 120
} as const;

export type PlayheadClockConfig = { readonly [K in keyof typeof PLAYHEAD_CLOCK_CONFIG]: number };

/** One authoritative position from the engine. */
export interface PlayheadSample {
	positionMs: number;
	/** `performance.now()`-domain instant at which `positionMs` was true at the output. */
	sampledAtMs: number;
	/** Track ms per real ms. */
	rate: number;
	playing: boolean;
	/** False while the presentation clock is stalled: freeze, never project. */
	trusted: boolean;
	/** Identity of what is loaded; a change is a discontinuity. */
	trackKey: string | null;
}

/** What `notePlayheadSample` did with a sample. Every reason but `slew`/`catch-up` lands exactly on the sample. */
export type PlayheadReconcile = 'first' | 'frozen' | 'slew' | 'catch-up' | 'snap-error' | 'snap-discontinuity';

export interface PlayheadClockState {
	anchor: PlayheadSample | null;
	/** Display minus truth at `offsetAtMs`, decaying linearly to 0 over `offsetWindowMs`. */
	offsetMs: number;
	offsetAtMs: number;
	offsetWindowMs: number;
	/** The last drift measured, display minus new truth (ms). */
	lastErrorMs: number;
}

export function initPlayheadClock(): PlayheadClockState {
	return { anchor: null, offsetMs: 0, offsetAtMs: 0, offsetWindowMs: 1, lastErrorMs: 0 };
}

/** Where the anchor says the deck is at `nowMs`, before any slew. */
function _projectedTruthMs(anchor: PlayheadSample, nowMs: number, cfg: PlayheadClockConfig = PLAYHEAD_CLOCK_CONFIG): number {
	if (!anchor.playing || !anchor.trusted) return anchor.positionMs;
	const elapsed = Math.min(cfg.maxProjectionMs, Math.max(-cfg.maxProjectionMs, nowMs - anchor.sampledAtMs));
	return anchor.positionMs + elapsed * anchor.rate;
}

/** Whether projecting to `nowMs` hit the `maxProjectionMs` cap (the clock stopped feeding samples). */
export function projectionCapped(anchor: PlayheadSample, nowMs: number, cfg: PlayheadClockConfig = PLAYHEAD_CLOCK_CONFIG): boolean {
	return anchor.playing && anchor.trusted && Math.abs(nowMs - anchor.sampledAtMs) > cfg.maxProjectionMs;
}

function _remainingOffsetMs(state: PlayheadClockState, nowMs: number): number {
	if (state.offsetMs === 0) return 0;
	const left = 1 - (nowMs - state.offsetAtMs) / state.offsetWindowMs;
	return left <= 0 ? 0 : state.offsetMs * Math.min(1, left);
}

/** The position to draw at `nowMs`; null before the first sample. */
export function playheadDisplayMs(state: PlayheadClockState, nowMs: number, cfg: PlayheadClockConfig = PLAYHEAD_CLOCK_CONFIG): number | null {
	if (state.anchor === null) return null;
	const truth = _projectedTruthMs(state.anchor, nowMs, cfg);
	if (!state.anchor.playing || !state.anchor.trusted) return truth;
	return truth + _remainingOffsetMs(state, nowMs);
}

function _isDiscontinuity(previous: PlayheadSample, next: PlayheadSample, cfg: PlayheadClockConfig): boolean {
	if (previous.trackKey !== next.trackKey) return true;
	if (!previous.playing || !previous.trusted) return true;
	const base = Math.max(Math.abs(previous.rate), 1e-9);
	return Math.abs(next.rate - previous.rate) / base >= cfg.tempoStepRatio;
}

function _snap(state: PlayheadClockState, sample: PlayheadSample, errorMs: number): void {
	state.anchor = sample;
	state.offsetMs = 0;
	state.lastErrorMs = errorMs;
}

/**
 * Take one authoritative sample at `nowMs` and reconcile the display to it.
 *
 * The drift is measured where it is visible: what the display shows at `nowMs`
 * against where the new anchor projects at `nowMs`. A slewed sample keeps that
 * drift as a decaying offset, so the frame drawn right after it shows exactly
 * what the frame before it would have: no jump.
 */
export function notePlayheadSample(
	state: PlayheadClockState,
	sample: PlayheadSample,
	nowMs: number,
	cfg: PlayheadClockConfig = PLAYHEAD_CLOCK_CONFIG
): PlayheadReconcile {
	const previous = state.anchor;
	if (previous === null) {
		_snap(state, sample, 0);
		return 'first';
	}
	if (!sample.playing || !sample.trusted) {
		_snap(state, sample, 0);
		return 'frozen';
	}
	const shown = playheadDisplayMs(state, nowMs, cfg) ?? sample.positionMs;
	const errorMs = shown - _projectedTruthMs(sample, nowMs, cfg);
	if (_isDiscontinuity(previous, sample, cfg)) {
		_snap(state, sample, errorMs);
		return 'snap-discontinuity';
	}
	const size = Math.abs(errorMs);
	if (size > cfg.snapErrorMs) {
		_snap(state, sample, errorMs);
		return 'snap-error';
	}
	state.anchor = sample;
	state.offsetMs = errorMs;
	state.offsetAtMs = nowMs;
	state.offsetWindowMs = size <= cfg.slewMaxErrorMs ? cfg.slewWindowMs : cfg.catchUpWindowMs;
	state.lastErrorMs = errorMs;
	return size <= cfg.slewMaxErrorMs ? 'slew' : 'catch-up';
}
