/**
 * PERFMODE-04 Q29: closed-loop pressure-adaptive cap scaling.
 *
 * A resource cap (prefetch tracks, prefetch bytes, ...) steps DOWN one
 * increment per elevated pressure tick and steps back UP one increment per
 * clear tick, so the sequence is genuinely closed-loop rather than a
 * two-state toggle: staying elevated for N ticks reaches a DIFFERENT cap
 * than staying elevated for N+1, and clearing steps back through the same
 * increments rather than snapping straight to baseline.
 *
 * Driven by whatever "elevated" signal the caller passes in (production
 * wires the same locked pressureIsElevated + xrun-delta signals
 * BACKGROUND_SHED_JOBS already uses - see prefetch-pressure-caps.ts). This
 * module itself takes no pressure threshold: `baseline` and `floor` are
 * already-locked PERFMODE-01 tier caps (e.g. SCALERS.STANDARD / SCALERS.LOW)
 * supplied by the caller, and PRESSURE_CAP_STEPS is a step-COUNT (how many
 * increments span baseline down to floor), not a pressure threshold.
 */

/** How many increments span baseline down to floor. Not a pressure threshold. */
export const PRESSURE_CAP_STEPS = 3;

export interface PressureCapState {
	readonly steps: number;
}

export function createPressureCapState(): PressureCapState {
	return { steps: 0 };
}

/** Next state as a function of the PREVIOUS state, not the raw signal. */
export function stepPressureCapState(state: PressureCapState, elevated: boolean): PressureCapState {
	const next = elevated
		? Math.min(PRESSURE_CAP_STEPS, state.steps + 1)
		: Math.max(0, state.steps - 1);
	return next === state.steps ? state : { steps: next };
}

/** baseline and floor are already-locked tier caps; only `state` moves. */
export function pressureScaledCap(baseline: number, floor: number, state: PressureCapState): number {
	if (state.steps <= 0) return baseline;
	const perStep = (baseline - floor) / PRESSURE_CAP_STEPS;
	return Math.max(floor, Math.round(baseline - perStep * state.steps));
}
