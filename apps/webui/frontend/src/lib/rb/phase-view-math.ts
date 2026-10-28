/**
 * Pure math behind the top bar's Phase scope and Phase meter views
 * (TOPVIEW-02, TOPVIEW-03). No DOM, no engine import: the views pass in a
 * deck's beatgrid and its playhead position, and draw what comes back.
 *
 * Every function returns null rather than a guess when it cannot measure
 * (no grid, a position before the first beat or after the last), so the
 * views can say "no beatgrid here" instead of parking a needle at zero,
 * which would read as "perfectly in phase".
 */
import type { AnlzBeat } from './anlz-types';
import type { TempoNormalization } from './beat-sync-math';
import { gridBeatPosition, lockBeatPosition } from './phase-lock';

/** Beats per bar the scope draws: the PQTZ grid counts n = 1..4. */
export const SCOPE_BAR_BEATS = 4;

export interface BarPhase {
	/** 0..1 through the bar; 0 is the downbeat (n == 1). */
	phase: number;
	/** The beat in the bar the position sits in, 1..4. */
	beatInBar: number;
}

/**
 * Where `positionSec` sits in its bar on `beats`. The bar comes from each
 * beat's own `n` (its number in the bar), not from counting beats since the
 * start, so a grid whose first beat is not a downbeat still reads true.
 */
export function barPhaseAt(beats: readonly AnlzBeat[], positionSec: number): BarPhase | null {
	const pos = gridBeatPosition(beats, positionSec);
	if (pos === null) return null;
	const index = Math.floor(pos);
	const n = beats[index]?.n;
	if (!Number.isInteger(n) || n < 1 || n > SCOPE_BAR_BEATS) return null;
	const frac = pos - index;
	return { phase: (n - 1 + frac) / SCOPE_BAR_BEATS, beatInBar: n };
}

/** The master deck's grid and playhead, which a folded follower's dial is read against. */
export interface ScopeMaster {
	beats: readonly AnlzBeat[];
	positionSec: number;
}

/**
 * The Phase scope dial for a deck Beat Sync locked at `lock` (its tempo fold,
 * follower grid beats per master beat), or null for a deck with no lock.
 * With no lock this is `barPhaseAt`, the deck's own bar numbers. A locked
 * follower has no bar of its own in the master's terms: the join may pair
 * any of its beats with any master beat (a BEAT join at exact tempo can put
 * master beat 2 on follower beat 4), so counting from its own downbeat
 * leaves a correctly locked pair a fixed fraction of a bar apart (Sol P1
 * 4302494490, 4349021851). Its dial is placed at the master's bar phase plus
 * its folded phase offset (`phaseOffsetBeats`), so a locked pair turns as
 * one dot and real drift shows as separation. That offset wraps at half a
 * beat (a quarter at 2x), the phase lock's own resolution. Null when there
 * is no master or either deck is off its grid.
 */
export function scopeBarPhase(
	beats: readonly AnlzBeat[],
	positionSec: number,
	lock: TempoNormalization | null,
	master: ScopeMaster | null
): BarPhase | null {
	if (lock === null) return barPhaseAt(beats, positionSec);
	if (master === null) return null;
	const masterBar = barPhaseAt(master.beats, master.positionSec);
	if (masterBar === null) return null;
	const offset = phaseOffsetBeats(master.beats, master.positionSec, beats, positionSec, lock);
	if (offset === null) return null;
	const turn = masterBar.phase + offset / SCOPE_BAR_BEATS;
	const phase = turn - Math.floor(turn);
	return { phase, beatInBar: Math.floor(phase * SCOPE_BAR_BEATS) + 1 };
}

/**
 * How far `follower`'s beat is from the master's nearest beat, in MASTER
 * beats: positive means the follower is AHEAD (early). Beat-level, not
 * bar-level, the way the phase lock measures it. `normalization` is follower
 * grid beats per master beat (Beat Sync's tempo fold, `beatSyncFold` in audio-engine):
 * a 0.5x or 2x follower walks its own grid at a different rate, so raw grid
 * positions drift apart while the decks are locked (Sol P1 4301475291). A
 * double-tempo follower meets a master beat on every one of its own beats, so
 * its phase points are half a master beat apart and the offset wraps to
 * [-0.25, 0.25]; otherwise to [-0.5, 0.5], exactly as `phaseErrorMs` wraps.
 */
export function phaseOffsetBeats(
	masterBeats: readonly AnlzBeat[],
	masterSec: number,
	followerBeats: readonly AnlzBeat[],
	followerSec: number,
	normalization: TempoNormalization
): number | null {
	// The lock's own grid (smoothed when flagged), so a locked pair reads 0 (Sol P1 4350124721).
	const m = lockBeatPosition(masterBeats, masterSec);
	const f = lockBeatPosition(followerBeats, followerSec);
	if (m === null || f === null) return null;
	const unit = Math.min(1, 1 / normalization);
	const diff = f / normalization - m;
	const wrapped = diff - unit * Math.round(diff / unit);
	// Math.round(0.5) is 1, so +half a unit wraps to -half; both mean the same.
	return Object.is(wrapped, -0) ? 0 : wrapped;
}

/** An offset in beats as wall-clock ms at the master's playing tempo. */
export function phaseOffsetMs(offsetBeats: number, masterBpm: number): number | null {
	if (!Number.isFinite(offsetBeats) || !Number.isFinite(masterBpm) || masterBpm <= 0) return null;
	return (offsetBeats * 60_000) / masterBpm;
}

/** A measured follower's Phase meter reading: the offset in ms at the master
 * tempo, or no-tempo when the master has no usable tempo (none, or not a
 * positive number), so the ms readout never quietly switches to beats
 * (Sol P1 4302183122). */
export type PhaseMeterReading =
	| { kind: 'no-tempo'; beats: number }
	| { kind: 'offset'; beats: number; ms: number };

export function phaseMeterReading(offsetBeats: number, masterBpm: number | null): PhaseMeterReading {
	const ms = masterBpm === null ? null : phaseOffsetMs(offsetBeats, masterBpm);
	if (ms === null) return { kind: 'no-tempo', beats: offsetBeats };
	return { kind: 'offset', beats: offsetBeats, ms };
}

/** The readout text for a reading: signed ms, or "no tempo". */
export function phaseMeterReadingText(reading: PhaseMeterReading): string {
	return reading.kind === 'offset' ? formatOffsetMs(reading.ms) : 'no tempo';
}

/** Signed ms text for the meter readout: "+12 ms", "-3 ms", "0 ms". */
export function formatOffsetMs(ms: number): string {
	const r = Math.round(ms);
	if (r === 0) return '0 ms';
	return `${r > 0 ? '+' : '-'}${Math.abs(r)} ms`;
}

/**
 * A point on a circle of radius `r` centered at (cx, cy) at `turn` (0..1)
 * clockwise from 12 o'clock, for SVG.
 */
export function pointOnCircle(cx: number, cy: number, r: number, turn: number): { x: number; y: number } {
	const a = turn * 2 * Math.PI;
	return { x: cx + r * Math.sin(a), y: cy - r * Math.cos(a) };
}
