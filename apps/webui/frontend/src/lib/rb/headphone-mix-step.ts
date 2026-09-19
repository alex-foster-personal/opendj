/** Normalized MIX positions (0 = full CUE, 1 = full MASTER). */
export const HEADPHONE_MIX_STEP = 1 / 3;

export type HeadphoneMixDirection = 1 | -1;

export interface HeadphoneMixStepResult {
	value: number;
	direction: HeadphoneMixDirection;
}

const MIX_ENDPOINTS = [0, HEADPHONE_MIX_STEP, 2 * HEADPHONE_MIX_STEP, 1] as const;
const MIX_ENDPOINT_EPSILON = 1e-10;

/** Collapse float drift at exact thirds and the 0/1 endpoints after a step. */
function normalizeMixStepValue(value: number): number {
	for (const target of MIX_ENDPOINTS) {
		if (Math.abs(value - target) < MIX_ENDPOINT_EPSILON) {
			return target;
		}
	}
	return value;
}

/**
 * Advance MIX by exactly one third in the remembered direction, reversing at 0 and 1.
 * From 0 the sequence is 0, 1/3, 2/3, 1, 2/3, 1/3, 0.
 */
export function stepHeadphoneMix(
	currentValue: number,
	direction: HeadphoneMixDirection
): HeadphoneMixStepResult {
	if (!Number.isFinite(currentValue)) {
		throw new Error(`headphone MIX value must be finite, got ${currentValue}`);
	}
	const clamped = Math.min(1, Math.max(0, currentValue));
	let nextDirection = direction;

	if (clamped >= 1 && nextDirection === 1) {
		nextDirection = -1;
	} else if (clamped <= 0 && nextDirection === -1) {
		nextDirection = 1;
	}

	let nextValue = normalizeMixStepValue(clamped + nextDirection * HEADPHONE_MIX_STEP);
	nextValue = Math.min(1, Math.max(0, nextValue));

	if (nextValue >= 1) {
		nextDirection = -1;
	} else if (nextValue <= 0) {
		nextDirection = 1;
	}

	return { value: nextValue, direction: nextDirection };
}

/**
 * The direction to step from next. A step's new direction only counts once
 * MIX actually reached the value it asked for: a rejected command (a preset
 * owning the controls, say) leaves MIX where it was, and adopting the
 * rejected step's reversal there would skip a position on the next click.
 */
export function committedMixDirection(
	committed: HeadphoneMixDirection,
	lastRequest: HeadphoneMixStepResult | null,
	currentValue: number
): HeadphoneMixDirection {
	if (lastRequest === null) return committed;
	return Math.abs(currentValue - lastRequest.value) < MIX_ENDPOINT_EPSILON ? lastRequest.direction : committed;
}
