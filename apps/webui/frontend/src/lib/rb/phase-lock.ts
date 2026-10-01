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
 * - past `PHASE_LOCK_RESEEK_BEATS` the lock is lost, and the caller re-runs
 *   the join (a seek) instead of trimming for tens of seconds.
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
import type { TempoNormalization } from '$lib/rb/beat-sync-math';

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

/**
 * Largest trim, as a fraction of the base tempo: 0.3%, 0.38 BPM at 128 BPM.
 * Below the pitch change a DJ hears on a varispeed deck (about 5 cents), and
 * twice the worst grid-rounding tempo error seen on real PQTZ (~0.15%, see
 * `_windowedIntervalBpm`), so a real drift of that size is always out-run.
 */
export const PHASE_LOCK_MAX_TRIM = 0.003;

/**
 * The wrapped phase error is at most half a beat; past a quarter of one the
 * lock is lost (a 117 ms flam at 128 BPM) and a capped trim would take ~40 s
 * to pull it back, so the caller re-seeks through the join instead.
 */
export const PHASE_LOCK_RESEEK_BEATS = 0.25;

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
	// The follower's position in MASTER beats, wrapped to the nearest one.
	const diff = f / (input.normalization ?? 1) - m;
	return (diff - Math.round(diff)) * _masterBeatWallMs(input, m);
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
	if (Math.abs(errorMs) > PHASE_LOCK_RESEEK_BEATS * beatWallMs) {
		return { action: 'reseek', tempo: base, errorMs };
	}
	const deadband = input.trimming ? PHASE_LOCK_RELEASE_MS : PHASE_LOCK_DEADBAND_MS;
	if (Math.abs(errorMs) < deadband) return { action: 'base', tempo: base, errorMs };
	// Ahead by E with W to fix it in: play slower by E/W.
	const correction = -errorMs / (PHASE_LOCK_CORRECTION_BEATS * beatWallMs);
	const trim = Math.max(-PHASE_LOCK_MAX_TRIM, Math.min(PHASE_LOCK_MAX_TRIM, correction));
	const range = input.pitchRangePct / 100;
	const tempo = Math.max(Math.max(0.01, 1 - range), Math.min(1 + range, base * (1 + trim)));
	return { action: 'trim', tempo, errorMs };
}

/** Whether `next` differs enough from the tempo last sent to send it. A return
 * to the exact base is always sent, so a trim never outlives its error. */
export function phaseLockShouldSend(lastSent: number, next: number, base: number): boolean {
	if (next === lastSent) return false;
	if (next === base) return true;
	return Math.abs(next - lastSent) > PHASE_LOCK_RESEND_RATIO * base;
}
