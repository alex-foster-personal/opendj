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
 *   instead of trimming an audible flam for tens of seconds.
 *
 * Phase is measured through EACH deck's own grid (fractional beat index from
 * its beat timestamps), so a variable grid is followed beat by beat rather
 * than through one BPM. No `$lib` state is read: both engines pass views.
 *
 * Wired in Rust engine mode (`phaseLockTick` in rust-transport.ts, on each
 * engine state frame) and in the Web Audio engine (`phase-lock-webaudio.ts`,
 * from the presentation tick in audio-engine.svelte.ts, with its own base-tempo
 * bookkeeping beside the scheduled revisions and re-anchor ramps).
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { beatTimeAt, gridBeatPosition, PHASE_LOCK_MAX_TRIM, type TempoNormalization } from '$lib/rb/beat-sync-math';

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
export { gridBeatPosition, PHASE_LOCK_MAX_TRIM };

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
}

export type PhaseLockDecision =
	/** Inside the deadband: play the base tempo. */
	| { action: 'base'; tempo: number; errorMs: number }
	/** A bounded corrective tempo. */
	| { action: 'trim'; tempo: number; errorMs: number }
	/** The lock is lost: re-run the join. `tempo` is the base meanwhile. */
	| { action: 'reseek'; tempo: number; errorMs: number }
	/** A position is off its grid (intro, outro): nothing to measure. */
	| { action: 'unmeasured'; tempo: number; errorMs: null };

/** One master beat in wall-clock ms at the master's position and tempo. */
function _masterBeatWallMs(input: PhaseLockInput, masterBeatPos: number): number {
	const i = Math.floor(masterBeatPos);
	return ((input.masterBeats[i + 1].t - input.masterBeats[i].t) / input.masterTempo) * 1000;
}

/**
 * The follower's signed phase error in wall-clock ms against the nearest
 * master beat phase: positive means the follower is AHEAD (early). Null when
 * either position is off its grid.
 */
export function phaseErrorMs(input: PhaseLockInput): number | null {
	const m = gridBeatPosition(input.masterBeats, input.masterPositionSec);
	const f = gridBeatPosition(input.followerBeats, input.followerPositionSec);
	if (m === null || f === null) return null;
	// The follower's position in MASTER beats, wrapped to the nearest phase
	// point. A double-tempo follower (normalization 2) meets a master beat on
	// EVERY one of its own beats, so its phase points are half a master beat
	// apart: wrapping to whole master beats read an odd-anchored, in-phase
	// follower as half a beat off and re-seeked it forever.
	const normalization = input.normalization ?? 1;
	const unit = Math.min(1, 1 / normalization);
	const diff = f / normalization - m;
	return (diff - unit * Math.round(diff / unit)) * _masterBeatWallMs(input, m);
}

/** The trim decision for one tick. Throws on a malformed input (a bug). */
export function phaseLockDecision(input: PhaseLockInput): PhaseLockDecision {
	const base = input.followerBaseTempo;
	if (!Number.isFinite(base) || base <= 0) {
		throw new RangeError(`phase lock: base tempo must be > 0, got ${base}`);
	}
	if (!Number.isFinite(input.masterTempo) || input.masterTempo <= 0) {
		throw new RangeError(`phase lock: master tempo must be > 0, got ${input.masterTempo}`);
	}
	const errorMs = phaseErrorMs(input);
	if (errorMs === null) return { action: 'unmeasured', tempo: base, errorMs: null };
	const m = gridBeatPosition(input.masterBeats, input.masterPositionSec) as number;
	const beatWallMs = _masterBeatWallMs(input, m);
	if (Math.abs(errorMs) > Math.min(PHASE_LOCK_RESEEK_BEATS * beatWallMs, PHASE_LOCK_RESEEK_MS)) {
		return { action: 'reseek', tempo: base, errorMs };
	}
	const deadband = input.trimming ? PHASE_LOCK_RELEASE_MS : PHASE_LOCK_DEADBAND_MS;
	if (Math.abs(errorMs) < deadband) return { action: 'base', tempo: base, errorMs };
	// Ahead by E with W to fix it in: play slower by E/W.
	const correction = -errorMs / (PHASE_LOCK_CORRECTION_BEATS * beatWallMs);
	const trim = Math.max(-PHASE_LOCK_MAX_TRIM, Math.min(PHASE_LOCK_MAX_TRIM, correction));
	return { action: 'trim', tempo: _inPitchRange(base * (1 + trim), input.pitchRangePct), errorMs };
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
	return _inPitchRange(asked, input.pitchRangePct);
}

/** Track seconds from beat index `at - half` to `at + half`; NaN when that
 * window leaves the grid (the caller then keeps its base). */
function _spanSec(beats: readonly AnlzBeat[], at: number, half: number): number {
	if (at - half < 0 || at + half > beats.length - 1) return NaN;
	return beatTimeAt(beats, at + half) - beatTimeAt(beats, at - half);
}

/** `tempo` held inside a +-`pitchRangePct` fader (never below 0.01). */
function _inPitchRange(tempo: number, pitchRangePct: number): number {
	const range = pitchRangePct / 100;
	return Math.max(0.01, 1 - range, Math.min(1 + range, tempo));
}

/** Whether `next` differs enough from the tempo last sent to send it. A return
 * to the exact base is always sent, so a trim never outlives its error. */
export function phaseLockShouldSend(lastSent: number, next: number, base: number): boolean {
	if (next === lastSent) return false;
	if (next === base) return true;
	return Math.abs(next - lastSent) > PHASE_LOCK_RESEND_RATIO * base;
}
