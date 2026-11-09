/**
 * D1 commanded-move planner (#9452, epic #9373).
 *
 * Plans a dial/fader glide from `from` to `to` starting at `nowMs` and ending on the Nth beat
 * boundary strictly after `nowMs`. Callers pass `beatTimesMs` in the same clock domain as
 * `nowMs` (the D3 runner presentation clock); this module only compares and adds.
 *
 * Gridless when the numeric grid is empty, non-finite, non-increasing, or has fewer than N
 * boundaries after `nowMs` (never extrapolates silently). Then `endMs = nowMs + beats *
 * fallbackBeatMs` and `gridless` is true. Use `beatTimesMsFromGrid` to build `beatTimesMs`
 * from deck ANLZ beats; it returns `[]` when `validateBeatGrid` rejects the grid.
 *
 * `beats === 0` is an instant jump: `endMs === startMs`, `gridless === false`.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { validateBeatGrid } from '$lib/rb/beat-sync-math';

export type CommandedMoveEasing = 'linear' | 'ease-in-out';

export interface CommandedMoveInput {
	from: number;
	to: number;
	/** Non-negative integer; 0 = jump. */
	beats: number;
	nowMs: number;
	beatTimesMs: readonly number[];
	/** One beat length when gridless; must be finite and > 0. */
	fallbackBeatMs: number;
	easing?: CommandedMoveEasing;
}

export interface CommandedMovePlan {
	readonly startMs: number;
	readonly endMs: number;
	/** True when the end came from `fallbackBeatMs` instead of the grid. */
	readonly gridless: boolean;
	valueAt(tMs: number): number;
}

function clamp01(v: number): number {
	if (v <= 0) return 0;
	if (v >= 1) return 1;
	return v;
}

function isUsableBeatGrid(beatTimesMs: readonly number[]): boolean {
	if (beatTimesMs.length === 0) return false;
	let prev = beatTimesMs[0];
	if (!Number.isFinite(prev)) return false;
	for (let i = 1; i < beatTimesMs.length; i++) {
		const t = beatTimesMs[i];
		if (!Number.isFinite(t) || t <= prev) return false;
		prev = t;
	}
	return true;
}

function firstBoundaryIndexStrictlyAfter(nowMs: number, beatTimesMs: readonly number[]): number {
	for (let i = 0; i < beatTimesMs.length; i++) {
		if (beatTimesMs[i] > nowMs) return i;
	}
	return beatTimesMs.length;
}

type EaseFn = (p: number) => number;

const EASING: Readonly<Record<CommandedMoveEasing, EaseFn>> = {
	linear: (p) => p,
	'ease-in-out': (p) => (1 - Math.cos(Math.PI * p)) / 2
};

/**
 * Deck grid to track-relative beat boundary times in ms.
 * Returns `[]` when `validateBeatGrid` throws so the planner falls back gridless.
 */
export function beatTimesMsFromGrid(beats: readonly AnlzBeat[]): number[] {
	try {
		validateBeatGrid(beats);
	} catch {
		return [];
	}
	return beats.map((b) => b.t * 1000);
}

export function planCommandedMove(input: CommandedMoveInput): CommandedMovePlan {
	const {
		from: fromRaw,
		to: toRaw,
		beats,
		nowMs,
		beatTimesMs,
		fallbackBeatMs,
		easing = 'linear'
	} = input;

	if (!Number.isFinite(nowMs)) {
		throw new RangeError('nowMs must be finite');
	}
	if (!Number.isInteger(beats) || beats < 0) {
		throw new RangeError('beats must be a non-negative integer');
	}
	if (!Number.isFinite(fallbackBeatMs) || fallbackBeatMs <= 0) {
		throw new RangeError('fallbackBeatMs must be finite and positive');
	}
	if (!Number.isFinite(fromRaw)) {
		throw new RangeError('from must be finite');
	}
	if (!Number.isFinite(toRaw)) {
		throw new RangeError('to must be finite');
	}
	if (!Object.prototype.hasOwnProperty.call(EASING, easing)) {
		throw new RangeError(`unknown easing: ${String(easing)}`);
	}
	if (!Array.isArray(beatTimesMs)) {
		throw new TypeError('beatTimesMs must be an array');
	}

	const from = clamp01(fromRaw);
	const to = clamp01(toRaw);
	const startMs = nowMs;
	const easeFn = EASING[easing];

	let endMs: number;
	let gridless: boolean;

	if (beats === 0) {
		endMs = startMs;
		gridless = false;
	} else if (isUsableBeatGrid(beatTimesMs)) {
		const i = firstBoundaryIndexStrictlyAfter(nowMs, beatTimesMs);
		const k = i + beats - 1;
		if (k < beatTimesMs.length) {
			endMs = beatTimesMs[k];
			gridless = false;
		} else {
			endMs = nowMs + beats * fallbackBeatMs;
			gridless = true;
		}
	} else {
		endMs = nowMs + beats * fallbackBeatMs;
		gridless = true;
	}

	const span = endMs - startMs;

	function valueAt(tMs: number): number {
		if (!Number.isFinite(tMs)) {
			throw new RangeError('tMs must be finite');
		}
		if (tMs >= endMs) {
			return to;
		}
		if (tMs <= startMs) {
			return from;
		}
		const p = (tMs - startMs) / span;
		const e = easeFn(p);
		return clamp01(from + (to - from) * e);
	}

	return { startMs, endMs, gridless, valueAt };
}
