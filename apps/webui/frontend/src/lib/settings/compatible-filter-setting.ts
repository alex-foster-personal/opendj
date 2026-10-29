/**
 * Settings rows for the compatible filter's modes and allowed key shift
 * (KEYSHIFT-01..05, the maintainer, Tue 13 Oct 2026). The library header dropdown flips
 * between the two modes; these rows define what Mashup means and the allowed
 * shift. Transition's key rule is the Camelot rule checkboxes in that dropdown. apply.ts
 * owns the prefs store and saves through the verified disk write
 * (patchCompatibleFilter), so this module stays a pure mapping and does not
 * add an importer to prefs.svelte.ts (the quality gate's fan-in ratchet).
 */
import type { CompatibleFilterPrefs } from '$lib/rb/compatible-filter-prefs';

export const COMPATIBLE_FILTER_SETTING_KEYS = [
	'compatible_filter.mode',
	'compatible_filter.mashup_relative',
	'compatible_filter.key_shift_max'
] as const;

export type CompatibleFilterSettingKey = (typeof COMPATIBLE_FILTER_SETTING_KEYS)[number];

export function readCompatibleFilterSetting(
	prefs: CompatibleFilterPrefs,
	key: CompatibleFilterSettingKey
): boolean | string {
	switch (key) {
		case 'compatible_filter.mode':
			return prefs.mode;
		case 'compatible_filter.mashup_relative':
			return prefs.mashup_relative;
		case 'compatible_filter.key_shift_max':
			return String(prefs.key_shift_max);
		default: {
			const _exhaustive: never = key;
			throw new Error(`Unhandled compatible filter setting: ${String(_exhaustive)}`);
		}
	}
}

/** The prefs patch for one row's value; a bad value throws at the call. */
export function compatibleFilterSettingPatch(
	key: CompatibleFilterSettingKey,
	value: boolean | string
): Partial<CompatibleFilterPrefs> {
	switch (key) {
		case 'compatible_filter.mode':
			if (value !== 'transition' && value !== 'mashup') {
				throw new Error(`compatible_filter.mode must be transition|mashup, got ${String(value)}`);
			}
			return { mode: value };
		case 'compatible_filter.mashup_relative':
			if (typeof value !== 'boolean') throw new Error(`${key} expects a boolean, got ${String(value)}`);
			return { mashup_relative: value };
		case 'compatible_filter.key_shift_max':
			if (value !== '0' && value !== '1' && value !== '2' && value !== '3') {
				throw new Error(`compatible_filter.key_shift_max must be 0|1|2|3, got ${String(value)}`);
			}
			return { key_shift_max: Number(value) as 0 | 1 | 2 | 3 };
		default: {
			const _exhaustive: never = key;
			throw new Error(`Unhandled compatible filter setting: ${String(_exhaustive)}`);
		}
	}
}
