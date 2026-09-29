/**
 * Gig helper user pref (PERFMODE-16).
 */

import { GIG_HELPER_PREFS, type GigHelperPref } from './gig-helper-prompt';

export type { GigHelperPref };
export { GIG_HELPER_PREFS };

export interface GigHelperPrefs {
	gig_helper: GigHelperPref;
}

export const GIG_HELPER_PREF_DEFAULTS: GigHelperPrefs = {
	gig_helper: 'unset'
};

export function validateGigHelperPrefField(
	parsed: Partial<Record<keyof GigHelperPrefs, unknown>>,
	storageKey: string
): Partial<GigHelperPrefs> {
	const value = parsed.gig_helper;
	if (value === undefined) return {};
	if (!(GIG_HELPER_PREFS as readonly unknown[]).includes(value)) {
		throw new Error(
			`${storageKey}: malformed prefs blob (gig_helper must be ` +
				`${GIG_HELPER_PREFS.join('|')}) - clear the localStorage key to recover`
		);
	}
	return { gig_helper: value as GigHelperPref };
}

export interface GigHelperPrefSetters {
	setGigHelper(next: GigHelperPref): void;
}

export function makeGigHelperPrefSetters(
	state: GigHelperPrefs,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<GigHelperPrefs>) => void
): GigHelperPrefSetters {
	return {
		setGigHelper(next) {
			state.gig_helper = next;
			persist();
			syncDiskPrefs({ gig_helper: next });
		}
	};
}

export function mergeGigHelperPrefsFromParsed(
	parsed: Partial<Record<keyof GigHelperPrefs, unknown>>,
	storageKey: string
): GigHelperPrefs {
	return { ...GIG_HELPER_PREF_DEFAULTS, ...validateGigHelperPrefField(parsed, storageKey) };
}

export function bindGigHelperPrefSetters(
	state: GigHelperPrefs,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<GigHelperPrefs>) => void
): GigHelperPrefSetters {
	return makeGigHelperPrefSetters(state, persist, syncDiskPrefs);
}
