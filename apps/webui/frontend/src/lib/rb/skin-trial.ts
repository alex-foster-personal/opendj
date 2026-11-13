/** Pure "try for 30 seconds" skin preview model. Time is passed in, no timers. */

export const TRIAL_MS = 30_000;

export type SkinTrialStatus = 'trying' | 'kept' | 'reverted' | 'cancelled';

export interface SkinTrialState {
	readonly prev: string;
	readonly next: string;
	readonly startedMs: number;
	readonly status: SkinTrialStatus;
	/** Skin that should be applied right now. */
	readonly active: string;
	/** Whole seconds left in the trial, rounded up; 0 once settled. */
	readonly secondsLeft: number;
}

export function startTrial(prev: string, next: string, nowMs: number): SkinTrialState {
	if (prev === next) {
		throw new Error(`skin trial: "${next}" is already the active skin`);
	}
	return {
		prev,
		next,
		startedMs: nowMs,
		status: 'trying',
		active: next,
		secondsLeft: TRIAL_MS / 1000
	};
}

export function tick(state: SkinTrialState, nowMs: number): SkinTrialState {
	if (state.status !== 'trying') return state;
	const leftMs = state.startedMs + TRIAL_MS - nowMs;
	if (leftMs <= 0) {
		return { ...state, status: 'reverted', active: state.prev, secondsLeft: 0 };
	}
	return { ...state, secondsLeft: Math.ceil(leftMs / 1000) };
}

export function keep(state: SkinTrialState): SkinTrialState {
	if (state.status !== 'trying') return state;
	return { ...state, status: 'kept', active: state.next, secondsLeft: 0 };
}

export function cancel(state: SkinTrialState): SkinTrialState {
	if (state.status !== 'trying') return state;
	return { ...state, status: 'cancelled', active: state.prev, secondsLeft: 0 };
}
