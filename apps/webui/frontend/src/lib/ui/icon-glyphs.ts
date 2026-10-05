/** Inline SVG path builders for chrome glyphs (CHROME-01). Use stroke="currentColor". */

export type MidiLabelGlyphKind = 'tick' | 'cross' | 'none';

export function midiLabelGlyphKind(
	status: 'green' | 'amber' | 'grey' | 'red'
): MidiLabelGlyphKind {
	if (status === 'green') return 'tick';
	if (status === 'red') return 'cross';
	return 'none';
}

/** MIDI entry status marks (CHROME-07): tick when a mapped controller is bound, cross when none is. */
export const MIDI_TICK_PATH = 'M2 6.2 5 9.2 10 3';

export const MIDI_CROSS_PATH = 'M3 3l6 6m0-6-6 6';

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

/** Playlist sidebar toggle target: the column browser (three panes). */
export const COLUMN_VIEW_PATH = 'M3 4h18v16H3z M9 4v16 M15 4v16';

/** Playlist sidebar toggle target: the tree list (stacked rows). */
export const TREE_LIST_PATH = 'M4 6h16 M4 12h16 M4 18h16';

/** Rename affordance (pencil). */
export const PENCIL_PATH = 'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z M14.5 5.5l4 4';

/** Lyrics column vocal verdict (a single note). */
export const MUSIC_NOTE_PATH = 'M9 18V5l11-2v13 M9 18a3 3 0 1 1-6 0a3 3 0 0 1 6 0z M20 16a3 3 0 1 1-6 0a3 3 0 0 1 6 0z';

/** Undo / redo (curved arrows), 24x24 viewBox, stroked. */
export const UNDO_PATH = 'M9 14 4 9l5-5 M4 9h10.5a5.5 5.5 0 0 1 0 11H11';

export const REDO_PATH = 'M15 14l5-5-5-5 M20 9H9.5a5.5 5.5 0 0 0 0 11H13';

/** Window controls, 24x24 viewBox, stroked: minimize bar, restore square, close X. */
export const MINIMIZE_PATH = 'M5 12h14';

export const RESTORE_PATH = 'M5 5h14v14H5z';

export const CLOSE_PATH = 'M6 6l12 12 M18 6 6 18';
