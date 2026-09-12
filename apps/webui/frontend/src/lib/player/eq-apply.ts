/**
 * LATENCY-03: immediate EQ ramp plan and apply, with no AudioContext import.
 *
 * Extracted from `audio-engine.svelte.ts` under convention D5 so the engine can
 * call one function at the AudioParam without growing its fan-out.
 */

export const EQ_APPLY_KIND = 'eq-apply';

export interface EqParam {
	value: number;
	cancelScheduledValues(t: number): void;
	setValueAtTime(v: number, t: number): void;
	linearRampToValueAtTime(v: number, t: number): void;
}

export function eqRampPlan(
	nowSec: number,
	rampSec: number
): { startSec: number; endSec: number } {
	return { startSec: nowSec, endSec: nowSec + rampSec };
}

export function applyEqRamp(
	param: EqParam,
	value: number,
	nowSec: number,
	rampSec: number
): { startSec: number; endSec: number } {
	const plan = eqRampPlan(nowSec, rampSec);
	param.cancelScheduledValues(plan.startSec);
	param.setValueAtTime(param.value, plan.startSec);
	param.linearRampToValueAtTime(value, plan.endSec);
	return plan;
}

export function eqApplyStages(input: {
	pressToApplyMs: number;
	rampStartOffsetMs: number;
	rampDurationMs: number;
}): Record<string, number> {
	return {
		press_to_apply_ms: input.pressToApplyMs,
		ramp_start_offset_ms: input.rampStartOffsetMs,
		ramp_duration_ms: input.rampDurationMs
	};
}
