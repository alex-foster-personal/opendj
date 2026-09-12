/**
 * Performance tier user pref (PERFMODE-01).
 */

import { applyExplicitPerfTierPref } from './perf-tier-client';

export const PERF_TIER_PREFS = ['auto', 'low', 'standard', 'high'] as const;
export type PerfTierPref = (typeof PERF_TIER_PREFS)[number];

export interface PerfTierPrefs {
	perf_tier: PerfTierPref;
}

export const PERF_TIER_PREF_DEFAULTS: PerfTierPrefs = {
	perf_tier: 'auto'
};

export function validatePerfTierPrefField(
	parsed: Partial<Record<keyof PerfTierPrefs, unknown>>,
	storageKey: string
): Partial<PerfTierPrefs> {
	const value = parsed.perf_tier;
	if (value === undefined) return {};
	if (!(PERF_TIER_PREFS as readonly unknown[]).includes(value)) {
		throw new Error(
			`${storageKey}: malformed prefs blob (perf_tier must be ` +
				`${PERF_TIER_PREFS.join('|')}) - clear the localStorage key to recover`
		);
	}
	return { perf_tier: value as PerfTierPref };
}

export interface PerfTierPrefSetters {
	setPerfTier(next: PerfTierPref): void;
}

export function makePerfTierPrefSetters(
	state: PerfTierPrefs,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<PerfTierPrefs>) => void,
	onTierChange?: (pref: PerfTierPref) => void
): PerfTierPrefSetters {
	return {
		setPerfTier(next) {
			state.perf_tier = next;
			persist();
			syncDiskPrefs({ perf_tier: next });
			onTierChange?.(next);
		}
	};
}

export function mergePerfTierPrefsFromParsed(
	parsed: Partial<Record<keyof PerfTierPrefs, unknown>>,
	storageKey: string
): PerfTierPrefs {
	return { ...PERF_TIER_PREF_DEFAULTS, ...validatePerfTierPrefField(parsed, storageKey) };
}

export function bindPerfTierPrefSetters(
	state: PerfTierPrefs,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<PerfTierPrefs>) => void
): PerfTierPrefSetters {
	return makePerfTierPrefSetters(state, persist, syncDiskPrefs, applyExplicitPerfTierPref);
}
