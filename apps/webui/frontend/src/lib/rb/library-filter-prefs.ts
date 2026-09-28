/**
 * The library filter checkboxes (Next / Remixes / Vocals / Available offline): type,
 * defaults, the fail-fast validator for the persisted blob, and the setters.
 * Split out of prefs.svelte.ts exactly the way lyrics-prefs.ts is - that
 * module sits against the 600-line file-size gate.
 *
 * Deliberately NOT a rune module (no $state here): pure types, validation and
 * a factory the reactive singleton in prefs.svelte.ts calls into.
 *
 * Agent parity is GET/PUT /api/v1/ui-prefs (disk-backed in
 * data/state/ui-prefs.json). The settings overlay is a second consumer of
 * the same setters, not the external agent path.
 */

interface LibraryFilterPrefs {
	/** Keep only tracks appropriate as next (Camelot + BPM window). Tab toggles it. */
	next_only_filter: boolean;
	/** Keep only rows whose title claims a remix / bootleg / rework / VIP. */
	remixes_filter: boolean;
	/** Keep only rows with real word-level lyrics over several lines. */
	vocals_filter: boolean;
	/** Keep only rows with local audio present (not cloud-only or streaming). */
	available_offline_filter: boolean;
}

/** All OFF: a fresh session that hid most of the library with no visible
 * cause would read as a broken load. */
export const LIBRARY_FILTER_PREF_DEFAULTS: LibraryFilterPrefs = {
	next_only_filter: false,
	remixes_filter: false,
	vocals_filter: false,
	available_offline_filter: false
};

const LIBRARY_FILTER_KEYS = [
	'next_only_filter',
	'remixes_filter',
	'vocals_filter',
	'available_offline_filter'
] as const;

/**
 * Validate the three filter fields of a parsed prefs blob. Each is optional -
 * an ABSENT key is a blob written before the key existed and the caller fills
 * its default; a PRESENT but wrong-typed key throws with `storageKey` in the
 * message. Only the present, validated keys come back.
 */
export function validateLibraryFilterPrefFields(
	parsed: Partial<Record<keyof LibraryFilterPrefs, unknown>>,
	storageKey: string
): Partial<LibraryFilterPrefs> {
	const out: Partial<LibraryFilterPrefs> = {};
	for (const key of LIBRARY_FILTER_KEYS) {
		const value = parsed[key];
		if (value === undefined) continue;
		if (typeof value !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (${key} is not a boolean) - ` +
					'clear the localStorage key to recover'
			);
		}
		out[key] = value;
	}
	return out;
}

interface LibraryFilterSetters {
	setNextOnlyFilter(next: boolean): void;
	toggleNextOnlyFilter(): void;
	setRemixesFilter(next: boolean): void;
	setVocalsFilter(next: boolean): void;
	setAvailableOfflineFilter(next: boolean): void;
}

/**
 * Build the setters against a live prefs state plus the caller's persist
 * hook. `state` is the caller's reactive $state object, passed by reference
 * and mutated in place - a Svelte 5 proxy stays reactive whichever module
 * holds the reference that mutates it (same contract as makeLyricsPrefSetters).
 */
export function makeLibraryFilterSetters(
	state: LibraryFilterPrefs,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<LibraryFilterPrefs>) => void
): LibraryFilterSetters {
	function _write<K extends keyof LibraryFilterPrefs>(key: K, next: LibraryFilterPrefs[K]): void {
		state[key] = next;
		persist();
		syncDiskPrefs({ [key]: next } as Partial<LibraryFilterPrefs>);
	}
	return {
		setNextOnlyFilter: (next) => _write('next_only_filter', next),
		toggleNextOnlyFilter: () => _write('next_only_filter', !state.next_only_filter),
		setRemixesFilter: (next) => _write('remixes_filter', next),
		setVocalsFilter: (next) => _write('vocals_filter', next),
		setAvailableOfflineFilter: (next) => _write('available_offline_filter', next)
	};
}
