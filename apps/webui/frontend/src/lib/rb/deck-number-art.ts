/**
 * ARTNUM-9649/9649b: the persisted "Deck number on artwork" pref and its size rule.
 * When on, each deck's track artwork (deck header, waveform gutter, mixer
 * column) carries the deck number as a white outline with a dark halo, so
 * the art identifies the deck. Default off so nothing changes for an
 * existing user. Spec: (private path).
 *
 * Split out of prefs.svelte.ts the way section-overlay-prefs.ts is. Pure
 * module: no runes, so the unit suite and stories import it directly.
 */
export const DECK_NUMBER_ART_VALUES = ['off', 'halo'] as const;
export type DeckNumberArt = (typeof DECK_NUMBER_ART_VALUES)[number];

export interface DeckNumberArtPrefsState {
	deck_number_art: DeckNumberArt;
}

export const DECK_NUMBER_ART_PREF_DEFAULTS: DeckNumberArtPrefsState = {
	deck_number_art: 'off'
};

/** Smallest live art square that still carries the overlay. The 22px
 * gutter (window under 800px tall) skips it: an 11px stroked glyph is mush. */
export const DECK_NUMBER_ART_MIN_PX = 40;

export function parseDeckNumberArt(value: unknown): DeckNumberArt | undefined {
	return (DECK_NUMBER_ART_VALUES as readonly unknown[]).includes(value)
		? (value as DeckNumberArt)
		: undefined;
}

/** Parse or throw, for the typed bus command and the settings apply path. */
export function requireDeckNumberArt(value: unknown, field: string): DeckNumberArt {
	const mode = parseDeckNumberArt(value);
	if (mode === undefined)
		throw new TypeError(
			`${field} must be ${DECK_NUMBER_ART_VALUES.join('|')}; got ${String(value)}`
		);
	return mode;
}

/** Validate the stored blob: absent keeps the default, anything else bad throws. */
export function validateDeckNumberArtPrefFields(
	parsed: { deck_number_art?: unknown },
	storageKey: string
): Partial<DeckNumberArtPrefsState> {
	if (parsed.deck_number_art === undefined) return {};
	const mode = parseDeckNumberArt(parsed.deck_number_art);
	if (mode === undefined) {
		throw new Error(
			`${storageKey}: malformed prefs blob (deck_number_art must be off|halo) - ` +
				'clear the localStorage key to recover'
		);
	}
	return { deck_number_art: mode };
}

/** True when the overlay draws on an art square of sizePx CSS pixels. */
export function deckNumberArtVisible(mode: DeckNumberArt, sizePx: number): boolean {
	return mode !== 'off' && sizePx >= DECK_NUMBER_ART_MIN_PX;
}

// ---------------------------------------------------------------------------
// Quality signal (spec: Proposed quality signal). WCAG contrast between the
// white stroke and the 90th-percentile relative luminance of the art under the
// glyph box. The halo is modeled as darkening the art to 0.35 of its luminance,
// the same model the spec measured (0 of 15 covers under 3:1, minimum 3.05).

/** WCAG non-text contrast floor, mimicked. */
export const DECK_NUMBER_ART_MIN_CONTRAST = 3;
/** Modeled effect of the dark halo on the art under the glyph. */
export const DECK_NUMBER_ART_HALO_LUMINANCE_FACTOR = 0.35;

/** WCAG 2 contrast ratio between two relative luminances in 0..1. */
export function wcagContrast(a: number, b: number): number {
	if (!(a >= 0 && a <= 1 && b >= 0 && b <= 1))
		throw new RangeError(`luminance out of 0..1: ${a}, ${b}`);
	return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/** Contrast of the white stroke over art whose p90 luminance is artP90, with or without the halo. */
export function strokeContrast(artP90: number, halo: boolean): number {
	return wcagContrast(1, halo ? artP90 * DECK_NUMBER_ART_HALO_LUMINANCE_FACTOR : artP90);
}
