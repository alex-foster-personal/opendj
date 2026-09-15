/**
 * Disk-synced app mode selection (PERFMODE-13).
 */

import type { AppModeId } from './app-mode';

export const APP_MODE_PREF_IDS = [
	'performance',
	'library-management',
	'library',
	'music-player'
] as const;

export interface AppModePrefs {
	app_mode: AppModeId;
}

export const APP_MODE_PREF_DEFAULTS: AppModePrefs = {
	app_mode: 'performance'
};

export function validateAppModePrefField(
	parsed: Partial<Record<keyof AppModePrefs, unknown>>,
	storageKey: string
): Partial<AppModePrefs> {
	const value = parsed.app_mode;
	if (value === undefined) return {};
	if (!(APP_MODE_PREF_IDS as readonly unknown[]).includes(value)) {
		throw new Error(
			`${storageKey}: malformed prefs blob (app_mode must be one of ` +
				`${APP_MODE_PREF_IDS.join(', ')}) - clear the localStorage key to recover`
		);
	}
	return { app_mode: value as AppModeId };
}

export interface AppModePrefSetters {
	setAppMode(next: AppModeId): void;
}

export function makeAppModePrefSetters(
	state: AppModePrefs,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<AppModePrefs>) => void
): AppModePrefSetters {
	return {
		setAppMode(next) {
			state.app_mode = next;
			persist();
			syncDiskPrefs({ app_mode: next });
		}
	};
}

export function mergeAppModePrefsFromParsed(
	parsed: Partial<Record<keyof AppModePrefs, unknown>>,
	storageKey: string
): Partial<AppModePrefs> {
	return validateAppModePrefField(parsed, storageKey);
}

export function bindAppModePrefSetters(
	state: AppModePrefs,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<AppModePrefs>) => void
): AppModePrefSetters {
	return makeAppModePrefSetters(state, persist, syncDiskPrefs);
}
