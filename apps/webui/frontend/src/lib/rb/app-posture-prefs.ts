/**
 * Gig vs Prep resource posture user pref (PERFMODE-03).
 */

import { applyPrefetchCaps } from '$lib/rb/audio-prefetch-cache.svelte';
import { setResolvedPosture, type AppPosture } from './app-posture';

export const APP_POSTURE_PREFS = ['prep', 'gig'] as const;
export type AppPosturePref = (typeof APP_POSTURE_PREFS)[number];

export interface AppPosturePrefs {
	app_posture: AppPosturePref;
}

export const APP_POSTURE_PREF_DEFAULTS: AppPosturePrefs = {
	app_posture: 'prep'
};

export function validateAppPosturePrefField(
	parsed: Partial<Record<keyof AppPosturePrefs, unknown>>,
	storageKey: string
): Partial<AppPosturePrefs> {
	const value = parsed.app_posture;
	if (value === undefined) return {};
	if (!(APP_POSTURE_PREFS as readonly unknown[]).includes(value)) {
		throw new Error(
			`${storageKey}: malformed prefs blob (app_posture must be ` +
				`${APP_POSTURE_PREFS.join('|')}) - clear the localStorage key to recover`
		);
	}
	return { app_posture: value as AppPosturePref };
}

export interface AppPosturePrefSetters {
	setAppPosture(next: AppPosturePref): void;
}

export function makeAppPosturePrefSetters(
	state: AppPosturePrefs,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<AppPosturePrefs>) => void,
	onPostureChange?: (pref: AppPosturePref) => void
): AppPosturePrefSetters {
	return {
		setAppPosture(next) {
			state.app_posture = next;
			persist();
			syncDiskPrefs({ app_posture: next });
			setResolvedPosture(next as AppPosture);
			applyPrefetchCaps();
			onPostureChange?.(next);
		}
	};
}

export function mergeAppPosturePrefsFromParsed(
	parsed: Partial<Record<keyof AppPosturePrefs, unknown>>,
	storageKey: string
): AppPosturePrefs {
	return { ...APP_POSTURE_PREF_DEFAULTS, ...validateAppPosturePrefField(parsed, storageKey) };
}

export function bindAppPosturePrefSetters(
	state: AppPosturePrefs,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<AppPosturePrefs>) => void
): AppPosturePrefSetters {
	return makeAppPosturePrefSetters(state, persist, syncDiskPrefs);
}
