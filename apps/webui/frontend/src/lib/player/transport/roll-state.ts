/**
 * FX roll stack and saved slip/loop state (FXROLL-01).
 *
 * Pure algebra for momentary beat loops that force SLIP. No engine imports.
 */

import type { LoopState } from '$lib/rb/deck-state-types';

/** Powers of two from 1/32 to 32 beats (Serato p. 52 range; Mixxx 2.1 sizes). */
export const ROLL_BEATS: readonly number[] = [
	1 / 32,
	1 / 16,
	1 / 8,
	1 / 4,
	1 / 2,
	1,
	2,
	4,
	8,
	16,
	32
];
export const ROLL_BEATS_LABEL = '1/32, 1/16, 1/8, 1/4, 1/2, 1, 2, 4, 8, 16, 32';
/** Deepest roll stack: one per pad on the 8-pad bounce bank. */
export const ROLL_STACK_MAX = 8;

export function isRollBeats(value: unknown): value is number {
	return (
		typeof value === 'number' &&
		Number.isFinite(value) &&
		ROLL_BEATS.some((choice) => choice === value)
	);
}

export function assertRollBeats(value: unknown): number {
	if (isRollBeats(value)) return value;
	throw new RangeError(
		`roll beats must be one of ${ROLL_BEATS_LABEL} (or null to release); got ${String(value)}`
	);
}

export interface RollEntry {
	beats: number;
	in_ms: number;
	out_ms: number;
}

/** What the roll overrode, captured at the first engage, restored at the last release. */
export interface RollSaved {
	slipEnabled: boolean;
	/** Slip was already running (user SLIP on plus a regular loop). */
	slipActive: boolean;
	/** The regular loop if one was ENGAGED, else null (copy, engaged: true). */
	loop: LoopState | null;
}

export interface RollState {
	saved: RollSaved;
	/** Bottom first. */
	stack: readonly RollEntry[];
}

function assertRollEntry(entry: RollEntry): RollEntry {
	assertRollBeats(entry.beats);
	if (!Number.isFinite(entry.in_ms) || !Number.isFinite(entry.out_ms)) {
		throw new RangeError('roll entry endpoints must be finite');
	}
	if (entry.in_ms >= entry.out_ms) {
		throw new RangeError(
			`roll entry in_ms must be less than out_ms, got ${entry.in_ms}..${entry.out_ms}`
		);
	}
	return { ...entry };
}

export function beginRoll(saved: RollSaved, entry: RollEntry): RollState {
	return {
		saved: { ...saved, loop: saved.loop === null ? null : { ...saved.loop } },
		stack: [assertRollEntry(entry)]
	};
}

export function pushRoll(state: RollState, entry: RollEntry): RollState {
	if (state.stack.length >= ROLL_STACK_MAX) {
		throw new RangeError(`roll stack is full (${ROLL_STACK_MAX} held rolls); release one first`);
	}
	const next = assertRollEntry(entry);
	return {
		saved: { ...state.saved, loop: state.saved.loop === null ? null : { ...state.saved.loop } },
		stack: [...state.stack, next]
	};
}

export type RollRelease =
	{ kind: 'resume'; entry: RollEntry; state: RollState } | { kind: 'end'; restore: RollSaved };

/** null in, null out: releasing with nothing held is a no-op. */
export function releaseRoll(state: RollState | null): RollRelease | null {
	if (state === null || state.stack.length === 0) return null;
	const stack = [...state.stack];
	stack.pop();
	if (stack.length > 0) {
		const entry = stack[stack.length - 1];
		return {
			kind: 'resume',
			entry: { ...entry },
			state: {
				saved: { ...state.saved, loop: state.saved.loop === null ? null : { ...state.saved.loop } },
				stack
			}
		};
	}
	return {
		kind: 'end',
		restore: { ...state.saved, loop: state.saved.loop === null ? null : { ...state.saved.loop } }
	};
}

/** Query mirror. roll_beats = top of stack or null; roll_stack = beats bottom first ([] when idle). */
export function rollQueryFields(state: RollState | null): {
	roll_beats: number | null;
	roll_stack: number[];
} {
	if (state === null || state.stack.length === 0) {
		return { roll_beats: null, roll_stack: [] };
	}
	return {
		roll_beats: state.stack[state.stack.length - 1].beats,
		roll_stack: state.stack.map((entry) => entry.beats)
	};
}
