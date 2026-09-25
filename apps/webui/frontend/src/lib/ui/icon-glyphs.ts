/** Inline SVG path builders for chrome glyphs (CHROME-01). Use stroke="currentColor". */

export type MidiLabelGlyphKind = 'tick' | 'cross' | 'none';

export function midiLabelGlyphKind(
	status: 'green' | 'amber' | 'grey' | 'red'
): MidiLabelGlyphKind {
	if (status === 'green') return 'tick';
	if (status === 'red') return 'cross';
	return 'none';
}

/** Sort arrow: up when asc, down when desc. */
export function sortArrowPath(asc: boolean): string {
	return asc ? 'M6 3 L10 9 L2 9 Z' : 'M6 11 L2 5 L10 5 Z';
}

export const STAR_FILLED_PATH =
	'M12 2l2.4 7.4H22l-6 4.6 2.3 7.2L12 17.8 5.7 21.2 8 13.6 2 9h7.6z';

export const STAR_OUTLINE_PATH =
	'M12 2l2.4 7.4H22l-6 4.6 2.3 7.2L12 17.8 5.7 21.2 8 13.6 2 9h7.6L12 2z';

export const RESCAN_PATH = 'M9.8 6 a3.8 3.8 0 1 1 -1.1 -2.7';

export const RESCAN_ARROW_PATH = 'M9.9 0.8 L9.9 3.6 L7.1 3.6 Z';

/** Play / plays column header triangle. */
export const PLAY_TRIANGLE_PATH = 'M4 3 L4 13 L12 8 Z';
