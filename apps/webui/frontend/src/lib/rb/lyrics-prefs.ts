/**
 * The six karaoke lyric prefs (PR-4 section C): type, defaults, the fail-fast
 * validators for the persisted blob, and the setters. Split out of
 * prefs.svelte.ts the same way deck-layout-prefs.ts splits the MORE/LESS
 * setters - that module sits against the 600-line file-size gate.
 *
 * Deliberately NOT a rune module (no $state here): pure types, validation and
 * factories the reactive singleton in prefs.svelte.ts calls into, so the
 * localStorage load path and the settings overlay share one definition.
 */

/** When word timings are pulled into memory for the library table. */
export const LYRICS_LOAD_STRATEGIES = ['in-view', 'hover', 'off'] as const;
export type LyricsLoadStrategy = (typeof LYRICS_LOAD_STRATEGIES)[number];

export interface LyricsPrefs {
	lyrics_global: boolean;
	lyrics_library_col: boolean;
	lyrics_hover_scrub: boolean;
	lyrics_load_strategy: LyricsLoadStrategy;
	lyrics_waveform_overlay: boolean;
	lyrics_deck_line: boolean;
}

/** Every lyric surface ships ON; the loading strategy defaults to the
 * middle setting (a 500ms hover) because preloading a whole playlist of word
 * timings is the RAM cost 'in-view' opts into deliberately. */
export const LYRICS_PREF_DEFAULTS: LyricsPrefs = {
	lyrics_global: true,
	lyrics_library_col: true,
	lyrics_hover_scrub: true,
	lyrics_load_strategy: 'hover',
	lyrics_waveform_overlay: true,
	lyrics_deck_line: true
};

/** The five boolean prefs - lyrics_load_strategy is the one enum. */
export const LYRICS_BOOLEAN_KEYS = [
	'lyrics_global',
	'lyrics_library_col',
	'lyrics_hover_scrub',
	'lyrics_waveform_overlay',
	'lyrics_deck_line'
] as const;

/**
 * Validate the six lyric fields of a parsed prefs blob. Every field is
 * optional - an ABSENT key is a blob written before the key existed and the
 * caller fills its default; a PRESENT but wrong-typed key throws with
 * `storageKey` in the message, so the text matches whichever storage key
 * backs the caller. Only the present, validated keys come back.
 */
export function validateLyricsPrefFields(
	parsed: Partial<Record<keyof LyricsPrefs, unknown>>,
	storageKey: string
): Partial<LyricsPrefs> {
	const out: Partial<LyricsPrefs> = {};
	for (const key of LYRICS_BOOLEAN_KEYS) {
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
	const strategy = parsed.lyrics_load_strategy;
	if (strategy !== undefined) {
		if (!(LYRICS_LOAD_STRATEGIES as readonly unknown[]).includes(strategy)) {
			throw new Error(
				`${storageKey}: malformed prefs blob (lyrics_load_strategy must be ` +
					`${LYRICS_LOAD_STRATEGIES.join('|')}) - clear the localStorage key to recover`
			);
		}
		out.lyrics_load_strategy = strategy as LyricsLoadStrategy;
	}
	return out;
}

export interface LyricsPrefSetters {
	setLyricsGlobal(next: boolean): void;
	toggleLyricsGlobal(): void;
	setLyricsLibraryCol(next: boolean): void;
	setLyricsHoverScrub(next: boolean): void;
	setLyricsLoadStrategy(next: LyricsLoadStrategy): void;
	setLyricsWaveformOverlay(next: boolean): void;
	setLyricsDeckLine(next: boolean): void;
}

/**
 * Build the six lyric setters against a live prefs state plus the caller's
 * persist / disk-sync hooks. Every setter writes localStorage AND PUTs
 * /api/v1/ui-prefs, so an agent driving the HTTP endpoint and an operator
 * clicking the overlay land on the same persisted state (agent parity).
 *
 * `state` is the caller's reactive $state object, passed by reference and
 * mutated in place - a Svelte 5 proxy stays reactive whichever module holds
 * the reference that mutates it (same contract as makeDeckLayoutSetters).
 */
export function makeLyricsPrefSetters(
	state: LyricsPrefs,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<LyricsPrefs>) => void
): LyricsPrefSetters {
	function _write<K extends keyof LyricsPrefs>(key: K, next: LyricsPrefs[K]): void {
		state[key] = next;
		persist();
		syncDiskPrefs({ [key]: next } as Partial<LyricsPrefs>);
	}
	return {
		setLyricsGlobal: (next) => _write('lyrics_global', next),
		toggleLyricsGlobal: () => _write('lyrics_global', !state.lyrics_global),
		setLyricsLibraryCol: (next) => _write('lyrics_library_col', next),
		setLyricsHoverScrub: (next) => _write('lyrics_hover_scrub', next),
		setLyricsLoadStrategy: (next) => _write('lyrics_load_strategy', next),
		setLyricsWaveformOverlay: (next) => _write('lyrics_waveform_overlay', next),
		setLyricsDeckLine: (next) => _write('lyrics_deck_line', next)
	};
}
