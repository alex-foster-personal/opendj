/** Canonical star count 0-5 for UI display and comparisons. Issue #2572. */

const POPM_TO_STARS: Readonly<Record<number, number>> = {
	51: 1,
	102: 2,
	153: 3,
	204: 4,
	255: 5
};

export function normalizeRating(value: number | null | undefined): number | null {
	if (value === null || value === undefined) return null;
	if (!Number.isFinite(value)) return null;
	if (!Number.isInteger(value)) return null;
	if (value >= 0 && value <= 5) return value;
	const mapped = POPM_TO_STARS[value];
	return mapped === undefined ? null : mapped;
}
