/**
 * STRUCT-05 section overlay colors: the six label families and the reader
 * for their --rb-section-* tokens. Split out of sections.ts so readPalette
 * (cues.ts), which every waveform surface calls, can resolve these colors
 * without pulling the section painter into the "/" first-paint bundle: the
 * painter is loaded on demand by sections-lazy.svelte.ts when the
 * show_sections pref is on.
 */

/** One color per DJ label family, plus the neutral fallback. */
export const SECTION_FAMILIES = ['edge', 'rise', 'drop', 'calm', 'body', 'neutral'] as const;
export type SectionFamily = (typeof SECTION_FAMILIES)[number];

/** Resolved section colors (the --rb-section-* tokens) plus label text color. */
export type SectionPalette = Record<SectionFamily, string> & { text: string };

const _SECTION_VARS: Record<SectionFamily, string> = {
	edge: '--rb-section-edge',
	rise: '--rb-section-rise',
	drop: '--rb-section-drop',
	calm: '--rb-section-calm',
	body: '--rb-section-body',
	neutral: '--rb-section-neutral'
};

/** Read the section tokens from an element's computed style. Fail-fast like
 * readPalette: an empty var means the element is outside .perf-root. */
export function readSectionPalette(styles: CSSStyleDeclaration, text: string): SectionPalette {
	const out = { text } as SectionPalette;
	for (const family of SECTION_FAMILIES) {
		const value = styles.getPropertyValue(_SECTION_VARS[family]).trim();
		if (value === '') {
			throw new Error(
				`section palette: CSS var ${_SECTION_VARS[family]} empty - element not under .perf-root?`
			);
		}
		out[family] = value;
	}
	return out;
}

