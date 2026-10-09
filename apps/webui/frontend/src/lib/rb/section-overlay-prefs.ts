/**
 * STRUCT-05: the persisted "Show sections" pref. When on, every waveform
 * surface paints each analyzed section (rekordbox PSSI phrase or own section)
 * as a translucent labeled band. Default OFF so nothing changes for an
 * existing user until they opt in.
 *
 * Split out of prefs.svelte.ts the same way jog-radial-prefs.ts is: that
 * module sits over the frontend file-size gate. Not a rune module - the
 * reactive singleton passes its $state object in by reference.
 */
export interface SectionOverlayPrefsState {
	show_sections: boolean;
}

export const SECTION_OVERLAY_PREF_DEFAULTS: SectionOverlayPrefsState = {
	show_sections: false
};

/** Validate the stored blob's show_sections: absent means a blob written
 * before the key existed (caller keeps the default); present but not a
 * boolean throws with the storage key named, like every other pref. */
export function validateSectionOverlayPrefFields(
	parsed: { show_sections?: unknown },
	storageKey: string
): Partial<SectionOverlayPrefsState> {
	const value = parsed.show_sections;
	if (value === undefined) return {};
	if (typeof value !== 'boolean') {
		throw new Error(
			`${storageKey}: malformed prefs blob (show_sections is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	return { show_sections: value };
}

export interface SectionOverlaySetters {
	setShowSections(next: boolean): void;
	toggleShowSections(): void;
}

/** Every setter writes localStorage AND PUTs /api/v1/ui-prefs, so an agent
 * driving the HTTP endpoint and an operator clicking the checkbox land on
 * the same persisted state (agent parity). */
export function makeSectionOverlaySetters(
	state: SectionOverlayPrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: { show_sections: boolean }) => void
): SectionOverlaySetters {
	function setShowSections(next: boolean): void {
		state.show_sections = next;
		persist();
		syncDiskPrefs({ show_sections: next });
	}
	return {
		setShowSections,
		toggleShowSections: () => setShowSections(!state.show_sections)
	};
}
