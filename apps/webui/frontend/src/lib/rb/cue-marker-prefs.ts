/**
 * Cue-marker visibility prefs (issue #10248 / WAVEQS-11730), persisted beside
 * show_sections and show_tempo_lane and plumbed the same way.
 */
import type { CueMarkerVisibility } from '$lib/components/rb/wave/cues';

export const CUE_MARKER_PREF_KEYS = [
	'show_hot_cue_markers',
	'show_memory_cue_markers',
	'show_loop_markers'
] as const;
export type CueMarkerPrefKey = (typeof CUE_MARKER_PREF_KEYS)[number];

export interface CueMarkerPrefsState {
	show_hot_cue_markers: boolean;
	show_memory_cue_markers: boolean;
	show_loop_markers: boolean;
}

export const CUE_MARKER_PREF_DEFAULTS: CueMarkerPrefsState = {
	show_hot_cue_markers: true,
	show_memory_cue_markers: true,
	show_loop_markers: true
};

export function validateCueMarkerPrefFields(
	parsed: Record<string, unknown>,
	storageKey: string
): Partial<CueMarkerPrefsState> {
	const out: Partial<CueMarkerPrefsState> = {};
	for (const key of CUE_MARKER_PREF_KEYS) {
		const value = parsed[key];
		if (value === undefined) continue;
		if (typeof value !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (${key} is not a boolean) - clear the localStorage key to recover`
			);
		}
		out[key] = value;
	}
	return out;
}

export interface CueMarkerSetters {
	setShowHotCueMarkers(next: boolean): void;
	setShowMemoryCueMarkers(next: boolean): void;
	setShowLoopMarkers(next: boolean): void;
}

export function makeCueMarkerSetters(
	state: CueMarkerPrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<CueMarkerPrefsState>) => void
): CueMarkerSetters {
	return {
		setShowHotCueMarkers(next: boolean): void {
			state.show_hot_cue_markers = next;
			persist();
			syncDiskPrefs({ show_hot_cue_markers: next });
		},
		setShowMemoryCueMarkers(next: boolean): void {
			state.show_memory_cue_markers = next;
			persist();
			syncDiskPrefs({ show_memory_cue_markers: next });
		},
		setShowLoopMarkers(next: boolean): void {
			state.show_loop_markers = next;
			persist();
			syncDiskPrefs({ show_loop_markers: next });
		}
	};
}

export function hydrateCueMarkerPrefs(
	body: Partial<Record<CueMarkerPrefKey, unknown>>,
	state: CueMarkerPrefsState
): void {
	for (const key of CUE_MARKER_PREF_KEYS) {
		const value = body[key];
		if (typeof value === 'boolean') state[key] = value;
	}
}

export function cueMarkerVisibility(state: CueMarkerPrefsState): CueMarkerVisibility {
	return {
		hot: state.show_hot_cue_markers,
		memory: state.show_memory_cue_markers,
		loop: state.show_loop_markers
	};
}
