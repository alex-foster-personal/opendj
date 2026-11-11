/**
 * Vocal lane visibility (issue #10238), persisted beside STRUCT-05's
 * show_sections and plumbed the same way (section-overlay-prefs.ts).
 *
 * - show_vocal_lane: paint the blue vocal bars on deck waveform rows and the
 *   deck overview strip. Default ON: analyzed vocal regions show as thin bars.
 */
export interface VocalLanePrefsState {
	show_vocal_lane: boolean;
}

export const VOCAL_LANE_PREF_DEFAULTS: VocalLanePrefsState = {
	show_vocal_lane: true
};

/** Validate the stored blob's vocal-lane field: absent means a blob written
 * before the key existed (caller keeps the default); present but malformed
 * throws with the storage key named, like every other pref. */
export function validateVocalLanePrefFields(
	parsed: { show_vocal_lane?: unknown },
	storageKey: string
): Partial<VocalLanePrefsState> {
	const out: Partial<VocalLanePrefsState> = {};
	const show = parsed.show_vocal_lane;
	if (show !== undefined) {
		if (typeof show !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (show_vocal_lane is not a boolean) - ` +
					'clear the localStorage key to recover'
			);
		}
		out.show_vocal_lane = show;
	}
	return out;
}

export interface VocalLaneSetters {
	setShowVocalLane(next: boolean): void;
}

/** Every setter writes localStorage AND PUTs /api/v1/ui-prefs, so an agent
 * driving the HTTP endpoint and an operator clicking the control land on
 * the same persisted state (agent parity). */
export function makeVocalLaneSetters(
	state: VocalLanePrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<VocalLanePrefsState>) => void
): VocalLaneSetters {
	return {
		setShowVocalLane(next: boolean): void {
			state.show_vocal_lane = next;
			persist();
			syncDiskPrefs({ show_vocal_lane: next });
		}
	};
}
