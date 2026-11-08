/**
 * PREF-01: normalise the tempo-pref popover's number fields.
 *
 * `bind:value` on `<input type="number">` writes `undefined` (or `null`) when a
 * field is emptied, so every empty value counts as unset here. The patch never
 * carries undefined members, which JSON.stringify would drop into `{}`.
 */
export type TempoPrefPatch = { regular: number | null; min: number | null; max: number | null };

export function tempoPrefFieldValue(raw: unknown): number | null {
	return typeof raw === 'number' && Number.isFinite(raw) ? raw : null;
}

export function tempoPrefPatchValue(
	regular: unknown,
	min: unknown,
	max: unknown
): TempoPrefPatch | null {
	const patch = {
		regular: tempoPrefFieldValue(regular),
		min: tempoPrefFieldValue(min),
		max: tempoPrefFieldValue(max)
	};
	// All three unset means "no preference": the API models that as null.
	return patch.regular === null && patch.min === null && patch.max === null ? null : patch;
}

export function tempoPrefRangeInvalid(min: unknown, max: unknown): boolean {
	const lo = tempoPrefFieldValue(min);
	const hi = tempoPrefFieldValue(max);
	return lo !== null && hi !== null && lo >= hi;
}
