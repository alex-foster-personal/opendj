/**
 * Continuous phase lock for Beat Sync followers (NAE-19).
 *
 * A join (`computeFollowerSyncPlan`) sets a follower's tempo ratio and phase
 * ONCE. Any small tempo mismatch after that - grid BPM against the audio's
 * real tempo, a stretcher's rate error, a windowed-interval tempo on a grid
 * that is not constant - then accumulates without bound: 0.05% is 150 ms, an
 * audible flam, after five minutes. This module closes the loop. Called
 * periodically for a locked, playing follower, it measures the follower's
 * beat-phase error against the master and returns a small corrective trim on
 * top of the follower's BASE sync tempo (the ratio the join chose):
 *
 * - inside `PHASE_LOCK_DEADBAND_MS` it returns the base exactly, so the trim
 *   never accumulates into a permanent tempo change and the loop does not
 *   hunt on the playhead feed's own jitter;
 * - outside it, a proportional nudge that would remove the error over
 *   `PHASE_LOCK_CORRECTION_BEATS` master beats, capped at
 *   `PHASE_LOCK_MAX_TRIM` of the base and never outside the pitch range;
 * - past `PHASE_LOCK_RESEEK_BEATS` or `PHASE_LOCK_RESEEK_MS`, whichever is
 *   smaller, the lock is lost, and the caller re-runs the join (a seek)
 *   instead of trimming an audible flam for tens of seconds. A re-join is
 *   deliberate: the error has to stay past the line for
 *   `PHASE_LOCK_REJOIN_CONFIRM_TICKS` ticks, and never sooner than
 *   `PHASE_LOCK_REJOIN_MIN_INTERVAL_SEC` after the last join. Until both
 *   hold, the capped trim keeps working on it.
 *
 * Phase is measured through EACH deck's own grid (fractional beat index from
 * its beat timestamps). An EVEN grid is read exactly as stored. An UNEVEN
 * grid (the shared rule, `classifyGrid`: suspect or variable_tempo) is read through a smoothed
 * copy of its beat times (`smoothedBeatTimes`): a rekordbox dynamic grid
 * stores beats in 5 or 10 ms steps that dither around a locally steady tempo,
 * and a lock that takes each stored beat as truth chases that dither with
 * trims and seeks (ADR deck-beatgrid-source, Amendment 1: a median 83 re-joins
 * a minute on the owner's 19 uneven grids). The grid itself is not changed:
 * only what the lock measures. With an uneven grid on either deck the base
 * tempo also FOLLOWS the smoothed local tempo (`PHASE_LOCK_TEMPO_FOLLOW_RATIO`),
 * so a real tempo change is tracked by a tempo move, not by letting the error
 * grow to the re-join line. No `$lib` state is read: both engines pass views.
 *
 * Wired in Rust engine mode (`phaseLockTick` in rust-transport.ts, on each
 * engine state frame) and in the Web Audio engine (`phase-lock-webaudio.ts`,
 * from the presentation tick in audio-engine.svelte.ts, with its own base-tempo
 * bookkeeping beside the scheduled revisions and re-anchor ramps).
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { beatTimeAt, PHASE_LOCK_MAX_TRIM, type TempoNormalization } from '$lib/rb/beat-sync-math';
import { classifyGrid, FLAGGED_GRID_CLASSES } from '$lib/rb/grid-quality';

/**
 * Errors smaller than this are left alone (the tempo goes back to base). The
 * Rust engine's playhead is extrapolated from a 30 Hz state feed, which jitters
 * by around a millisecond; two decks within 2 ms are far inside what an ear
 * hears as one hit (a flam starts near 10 ms).
 */
export const PHASE_LOCK_DEADBAND_MS = 2;

/**
 * Hysteresis: once trimming, the trim holds until the error is back under
 * this, not merely under the deadband. Without it an error sitting on the
 * deadband edge flips trim/base on the feed's jitter, a command per tick.
 */
export const PHASE_LOCK_RELEASE_MS = 1;

/**
 * The proportional gain: correct the measured error over this many master
 * beats (1.9 s at 128 BPM). A rate error R then settles at R times that
 * window: 0.15% holds near 3 ms. A shorter window holds tighter but turns
 * +-0.5 ms of feed jitter into tempo noise and sends more commands (two beats
 * sent about twice as many in the simulation; eight held 0.15% only near 6 ms).
 */
export const PHASE_LOCK_CORRECTION_BEATS = 4;

// Largest trim (0.3% of base); defined in beat-sync-math.ts, see there.
export { PHASE_LOCK_MAX_TRIM };

/**
 * The wrapped phase error is at most half a beat; past a quarter of one the
 * lock is lost (a 117 ms flam at 128 BPM) and a capped trim would take ~40 s
 * to pull it back, so the caller re-seeks through the join instead.
 */
export const PHASE_LOCK_RESEEK_BEATS = 0.25;

/**
 * ...and, whatever the tempo, past this many ms. The capped trim corrects
 * only PHASE_LOCK_MAX_TRIM of a second per second (3 ms/s), so the quarter
 * beat alone left every error between a flam (~10 ms) and 117 ms to be walked
 * back audibly for up to ~34 s. At 15 ms a trimmed error is under the 10 ms
 * flam line within ~1.7 s; anything larger is one re-join instead.
 */
export const PHASE_LOCK_RESEEK_MS = 15;

/** A new trim is sent only when it moved by more than this fraction of the
 * base (or returns to base), so the loop does not send one command per tick:
 * about two a second in the simulation, against 30 state frames. */
export const PHASE_LOCK_RESEND_RATIO = 0.0005;

/**
 * Half-width, in beats, of the window an uneven grid's beat times are
 * smoothed over: each beat is replaced by a least-squares line through the
 * 17 beats centered on it (about four bars), read at that beat. The grid is
 * known ahead of the playhead, so the window is CENTERED and the smoothing has
 * no lag: a tempo change is followed as it happens, rounded over the window.
 * 10 ms storage dither is cut to about a millisecond. Measured on the 19
 * uneven grids (ops/beatbench/phase-lock-jitter-round-1): 4, 8 and 16 give the
 * same re-join count; 16 leaves more of a real tempo step unfollowed.
 */
export const PHASE_LOCK_SMOOTH_HALF_WINDOW_BEATS = 8;

/**
 * With an uneven grid on either deck, the base tempo moves to the smoothed
 * local tempo once that is further than this fraction from it. The same size
 * as the smallest trim worth sending, so the base does not step on storage
 * noise and a real tempo move is taken at once.
 */
export const PHASE_LOCK_TEMPO_FOLLOW_RATIO = PHASE_LOCK_RESEND_RATIO;

/**
 * No lock-initiated re-join sooner than this after the last join. At the cap
 * the trim removes 3 ms a second, 24 ms in this window, which is more than
 * the whole PHASE_LOCK_RESEEK_MS line: an error a trim can walk back in a
 * bounded time is trimmed, and a seek is what is left for the rest. It also
 * bounds the worst case at 7.5 seeks a minute on a grid nothing can hold.
 */
export const PHASE_LOCK_REJOIN_MIN_INTERVAL_SEC = 8;

/**
 * The error has to be past the re-join line on this many consecutive ticks
 * (0.1 s at 30 Hz) before a re-join is asked for, so one bad position reading
 * is never a seek.
 */
export const PHASE_LOCK_REJOIN_CONFIRM_TICKS = 3;

export interface PhaseLockInput {
	masterBeats: readonly AnlzBeat[];
	masterPositionSec: number;
	/** The master's playing tempo ratio. */
	masterTempo: number;
	followerBeats: readonly AnlzBeat[];
	followerPositionSec: number;
	/** The tempo ratio the join chose: the trim is always relative to it. */
	followerBaseTempo: number;
	/** Follower grid intervals per master beat (the join plan's). Default 1. */
	normalization?: TempoNormalization;
	/** The follower's pitch range in percent (8 = +-8%). */
	pitchRangePct: number;
	/** A trim is in force now (the last tempo sent is not the base). */
	trimming?: boolean;
	/** Seconds since this follower's last join (any join re-records the lock). */
	sinceJoinSec: number;
	/** `overLineTicks` of the previous decision for this lock (0 for a new lock). */
	overLineTicks: number;
	/**
	 * The phase offset the DJ has dialed in since the join (jog or nudge), in
	 * wall-clock ms, positive = follower ahead. The lock holds the follower
	 * THERE instead of pulling it back onto the master's beat. Default 0: the
	 * master's beat itself.
	 */
	userOffsetMs?: number;
}

/** Carried by every decision: what the caller stores on its lock. */
interface PhaseLockCarry {
	/** The base tempo now: the input's, or the smoothed local tempo it moved to. */
	base: number;
	/** Consecutive ticks the error has been past the re-join line. */
	overLineTicks: number;
}

export type PhaseLockDecision = PhaseLockCarry &
	(
		| /** Inside the deadband: play the base tempo. */
		  { action: 'base'; tempo: number; errorMs: number }
		/** A bounded corrective tempo. */
		| { action: 'trim'; tempo: number; errorMs: number }
		/** The lock is lost: re-run the join. `tempo` is the base meanwhile. */
		| { action: 'reseek'; tempo: number; errorMs: number }
		/** A position is off its grid (intro, outro): nothing to measure. */
		| { action: 'unmeasured'; tempo: number; errorMs: null }
	);

/** Fractional beat index of `positionSec` on `beats`, or null off the grid. */
export function gridBeatPosition(beats: readonly AnlzBeat[], positionSec: number): number | null {
	const n = beats.length;
	if (n < 2 || !Number.isFinite(positionSec)) return null;
	if (positionSec < beats[0].t || positionSec >= beats[n - 1].t) return null;
	// Last beat at or before the position.
	let lo = 0;
	let hi = n - 1;
	while (hi - lo > 1) {
		const mid = (lo + hi) >> 1;
		if (beats[mid].t <= positionSec) lo = mid;
		else hi = mid;
	}
	const span = beats[lo + 1].t - beats[lo].t;
	if (!(span > 0)) return null;
	return lo + (positionSec - beats[lo].t) / span;
}

// ------------------------------------------------ what the lock measures

/**
 * `beats` with each time replaced by a least-squares line through the beats
 * within `halfWindowBeats` of it, read at that beat. Near either end the
 * window is whatever part of it exists. Pure; no beat is added or dropped.
 */
export function smoothedBeatTimes(
	beats: readonly AnlzBeat[],
	halfWindowBeats: number = PHASE_LOCK_SMOOTH_HALF_WINDOW_BEATS
): Float64Array {
	if (!Number.isInteger(halfWindowBeats) || halfWindowBeats < 1) {
		throw new RangeError(`phase lock: smoothing half window must be >= 1 beat, got ${halfWindowBeats}`);
	}
	const stored = new Float64Array(beats.length);
	for (let i = 0; i < stored.length; i++) stored[i] = beats[i].t;
	return _smoothedTimes(stored, halfWindowBeats);
}

/** `smoothedBeatTimes` over plain times (one read of each beat, off any state proxy). */
function _smoothedTimes(stored: Float64Array, halfWindowBeats: number): Float64Array {
	const n = stored.length;
	const out = new Float64Array(n);
	for (let i = 0; i < n; i++) {
		const first = Math.max(0, i - halfWindowBeats);
		const last = Math.min(n - 1, i + halfWindowBeats);
		const count = last - first + 1;
		const origin = stored[i];
		// Least squares of (t - origin) on (index - i); the value at i is the intercept.
		let sumX = 0;
		let sumY = 0;
		let sumXX = 0;
		let sumXY = 0;
		for (let j = first; j <= last; j++) {
			const x = j - i;
			const y = stored[j] - origin;
			sumX += x;
			sumY += y;
			sumXX += x * x;
			sumXY += x * y;
		}
		const spread = count * sumXX - sumX * sumX;
		const slope = spread > 0 ? (count * sumXY - sumX * sumY) / spread : 0;
		out[i] = origin + (sumY - slope * sumX) / count;
	}
	return out;
}

/** A grid as the lock reads it: stored times when even, smoothed when not. */
interface LockGrid {
	times: Float64Array;
	smoothed: boolean;
	/** The source grid this was built from, to notice an in-place edit. */
	firstSec: number;
	lastSec: number;
}

const _lockGrids = new WeakMap<readonly AnlzBeat[], LockGrid>();

function _lockGrid(beats: readonly AnlzBeat[]): LockGrid | null {
	const n = beats.length;
	if (n < 2) return null;
	const cached = _lockGrids.get(beats);
	if (
		cached !== undefined &&
		cached.times.length === n &&
		cached.firstSec === beats[0].t &&
		cached.lastSec === beats[n - 1].t
	) {
		return cached;
	}
	const stored = new Float64Array(n);
	for (let i = 0; i < n; i++) stored[i] = beats[i].t;
	// The ONE rule the deck badge and the library flag read (GRIDFLAG-01).
	const smoothed = FLAGGED_GRID_CLASSES.includes(classifyGrid(Array.from(stored)).gridClass);
	const times = smoothed ? _smoothedTimes(stored, PHASE_LOCK_SMOOTH_HALF_WINDOW_BEATS) : stored;
	const grid: LockGrid = { times, smoothed, firstSec: beats[0].t, lastSec: beats[n - 1].t };
	_lockGrids.set(beats, grid);
	return grid;
}

/** `gridBeatPosition` over a lock grid's times. */
function _lockBeatPosition(times: Float64Array, positionSec: number): number | null {
	const n = times.length;
	if (!Number.isFinite(positionSec)) return null;
	if (positionSec < times[0] || positionSec >= times[n - 1]) return null;
	let lo = 0;
	let hi = n - 1;
	while (hi - lo > 1) {
		const mid = (lo + hi) >> 1;
		if (times[mid] <= positionSec) lo = mid;
		else hi = mid;
	}
	const span = times[lo + 1] - times[lo];
	if (!(span > 0)) return null;
	return lo + (positionSec - times[lo]) / span;
}

/**
 * Seconds per beat around `beatPos`. A smoothed grid's own interval is already
 * a window average. An even grid's single interval carries up to a millisecond
 * of storage rounding (0.2% at 128 BPM), so it is averaged over the same window.
 */
function _localPeriodSec(grid: LockGrid, beatPos: number): number {
	const last = grid.times.length - 1;
	const i = Math.min(Math.floor(beatPos), last - 1);
	if (grid.smoothed) return grid.times[i + 1] - grid.times[i];
	const from = Math.max(0, i - PHASE_LOCK_SMOOTH_HALF_WINDOW_BEATS);
	const to = Math.min(last, i + 1 + PHASE_LOCK_SMOOTH_HALF_WINDOW_BEATS);
	return (grid.times[to] - grid.times[from]) / (to - from);
}

/** What one tick reads off the two grids. */
interface PhaseMeasure {
	/** Signed error against the held phase, wall-clock ms; positive = follower ahead. */
	errorMs: number;
	/** One master beat in wall-clock ms at the master's position and tempo. */
	beatWallMs: number;
	/** The follower tempo that keeps this phase, from both grids' local tempo. */
	localTempo: number;
	/** Either grid is read smoothed, so the base follows `localTempo`. */
	smoothed: boolean;
}

function _measure(input: PhaseLockInput): PhaseMeasure | null {
	const master = _lockGrid(input.masterBeats);
	const follower = _lockGrid(input.followerBeats);
	if (master === null || follower === null) return null;
	const m = _lockBeatPosition(master.times, input.masterPositionSec);
	const f = _lockBeatPosition(follower.times, input.followerPositionSec);
	if (m === null || f === null) return null;
	const mi = Math.floor(m);
	const beatWallMs = ((master.times[mi + 1] - master.times[mi]) / input.masterTempo) * 1000;
	// The follower's position in MASTER beats, wrapped to the nearest phase
	// point. A double-tempo follower (normalization 2) meets a master beat on
	// EVERY one of its own beats, so its phase points are half a master beat
	// apart: wrapping to whole master beats read an odd-anchored, in-phase
	// follower as half a beat off and re-seeked it forever.
	const normalization = input.normalization ?? 1;
	const unit = Math.min(1, 1 / normalization);
	// The phase the lock holds is the master's beat plus whatever the DJ dialed in.
	const diff = f / normalization - m - (input.userOffsetMs ?? 0) / beatWallMs;
	const smoothed = master.smoothed || follower.smoothed;
	return {
		errorMs: (diff - unit * Math.round(diff / unit)) * beatWallMs,
		beatWallMs,
		// `normalization` follower beats pass per master beat.
		localTempo: smoothed
			? (input.masterTempo * normalization * _localPeriodSec(follower, f)) / _localPeriodSec(master, m)
			: input.followerBaseTempo,
		smoothed
	};
}

/**
 * The follower's signed phase error in wall-clock ms against the phase the
 * lock holds (the nearest master beat phase, plus `userOffsetMs`): positive
 * means the follower is AHEAD (early). Read through each grid as the lock
 * reads it (an uneven grid smoothed). Null when either position is off its grid.
 */
export function phaseErrorMs(input: PhaseLockInput): number | null {
	return _measure(input)?.errorMs ?? null;
}

/** The trim decision for one tick. Throws on a malformed input (a bug). */
export function phaseLockDecision(input: PhaseLockInput): PhaseLockDecision {
	let base = input.followerBaseTempo;
	if (!Number.isFinite(base) || base <= 0) {
		throw new RangeError(`phase lock: base tempo must be > 0, got ${base}`);
	}
	if (!Number.isFinite(input.masterTempo) || input.masterTempo <= 0) {
		throw new RangeError(`phase lock: master tempo must be > 0, got ${input.masterTempo}`);
	}
	if (!Number.isFinite(input.userOffsetMs ?? 0)) {
		throw new RangeError(`phase lock: user offset must be finite, got ${input.userOffsetMs}`);
	}
	if (!(input.sinceJoinSec >= 0)) {
		throw new RangeError(`phase lock: seconds since the join must be >= 0, got ${input.sinceJoinSec}`);
	}
	if (!Number.isInteger(input.overLineTicks) || input.overLineTicks < 0) {
		throw new RangeError(`phase lock: over-line ticks must be an integer >= 0, got ${input.overLineTicks}`);
	}
	const measure = _measure(input);
	if (measure === null) return { action: 'unmeasured', tempo: base, errorMs: null, base, overLineTicks: 0 };
	const { errorMs, beatWallMs } = measure;
	const range = input.pitchRangePct / 100;
	const inRange = (tempo: number): number => Math.max(Math.max(0.01, 1 - range), Math.min(1 + range, tempo));
	// A real tempo change on a smoothed grid is followed by MOVING the base,
	// never by letting the phase error grow until the trim cap or a seek.
	if (measure.smoothed) {
		const local = inRange(measure.localTempo);
		if (Math.abs(local / base - 1) > PHASE_LOCK_TEMPO_FOLLOW_RATIO) base = local;
	}
	let overLineTicks = 0;
	if (Math.abs(errorMs) > Math.min(PHASE_LOCK_RESEEK_BEATS * beatWallMs, PHASE_LOCK_RESEEK_MS)) {
		overLineTicks = input.overLineTicks + 1;
		if (
			overLineTicks >= PHASE_LOCK_REJOIN_CONFIRM_TICKS &&
			input.sinceJoinSec >= PHASE_LOCK_REJOIN_MIN_INTERVAL_SEC
		) {
			return { action: 'reseek', tempo: base, errorMs, base, overLineTicks };
		}
		// Not yet a re-join: the capped trim below works on it meanwhile.
	}
	const deadband = input.trimming ? PHASE_LOCK_RELEASE_MS : PHASE_LOCK_DEADBAND_MS;
	if (Math.abs(errorMs) < deadband) return { action: 'base', tempo: base, errorMs, base, overLineTicks };
	// Ahead by E with W to fix it in: play slower by E/W.
	const correction = -errorMs / (PHASE_LOCK_CORRECTION_BEATS * beatWallMs);
	const trim = Math.max(-PHASE_LOCK_MAX_TRIM, Math.min(PHASE_LOCK_MAX_TRIM, correction));
	return { action: 'trim', tempo: inRange(base * (1 + trim)), errorMs, base, overLineTicks };
}

/** Master beats over which the feed-forward base measures both grids,
 * centered on the playheads (two behind, two ahead). */
export const PHASE_LOCK_FEED_FORWARD_BEATS = 4;

/** The feed-forward base moves only when the grids ask for more than this
 * fraction of it: half the trim cap. Under it the trim absorbs the residual;
 * rekordbox's millisecond beat times dither a constant grid's measured tempo
 * by up to ~0.1% over the window, which must not move the base. */
export const PHASE_LOCK_FEED_FORWARD_HYSTERESIS = PHASE_LOCK_MAX_TRIM / 2;

/**
 * The follower's base tempo as the two grids ask for it HERE (NAE-19 F4):
 * the ratio that makes the follower cover `normalization` of its grid beats
 * per master beat over `PHASE_LOCK_FEED_FORWARD_BEATS` master beats centered
 * on the playheads, at the master's playing tempo. A join fixes the base
 * once and the trim can only move `PHASE_LOCK_MAX_TRIM` around it, so a
 * tempo change INSIDE either grid (a ramp, a second const_region) used to
 * become a run of audible re-seeks. A window, not one interval, so
 * ms-rounded beat times do not become tempo noise; centered, not ahead, so a
 * step's phase error is split before and after it (about 6 ms for a 3% step
 * at 128 BPM, under the re-seek line; a window ahead met it early and
 * re-seeked). Returns `input.followerBaseTempo` unchanged when either window
 * leaves its grid or the grids ask for less than
 * `PHASE_LOCK_FEED_FORWARD_HYSTERESIS` of change; kept inside the pitch range.
 */
export function phaseLockFeedForwardBase(input: Omit<PhaseLockInput, 'trimming'>): number {
	const base = input.followerBaseTempo;
	const m = gridBeatPosition(input.masterBeats, input.masterPositionSec);
	const f = gridBeatPosition(input.followerBeats, input.followerPositionSec);
	if (m === null || f === null) return base;
	const half = PHASE_LOCK_FEED_FORWARD_BEATS / 2;
	const asked =
		(_spanSec(input.followerBeats, f, half * (input.normalization ?? 1)) * input.masterTempo) /
		_spanSec(input.masterBeats, m, half);
	if (!(asked > 0 && asked < Infinity)) return base; // also refuses NaN
	if (Math.abs(asked - base) <= PHASE_LOCK_FEED_FORWARD_HYSTERESIS * base) return base;
	const range = input.pitchRangePct / 100;
	return Math.max(Math.max(0.01, 1 - range), Math.min(1 + range, asked));
}

/** Track seconds from beat index `at - half` to `at + half`; NaN when that
 * window leaves the grid (the caller then keeps its base). */
function _spanSec(beats: readonly AnlzBeat[], at: number, half: number): number {
	if (at - half < 0 || at + half > beats.length - 1) return NaN;
	return beatTimeAt(beats, at + half) - beatTimeAt(beats, at - half);
}

/** Whether `next` differs enough from the tempo last sent to send it. A return
 * to the exact base is always sent, so a trim never outlives its error. */
export function phaseLockShouldSend(lastSent: number, next: number, base: number): boolean {
	if (next === lastSent) return false;
	if (next === base) return true;
	return Math.abs(next - lastSent) > PHASE_LOCK_RESEND_RATIO * base;
}
