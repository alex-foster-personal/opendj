/**
 * One name and one hover sentence per library filter, shared by every surface
 * that shows the filters: the library's checkbox row (BrowserPanel) and the
 * Cmd K bar's icon row (CMDK-05). the maintainer, Thu 8 Oct 2026: "same library wording
 * unless new, use same variables for names of filters throughout app".
 *
 * A rename lands here once and both surfaces follow. The Vocals hover text in
 * the library is computed (it counts unknown rows), so `tip` is the static
 * part both surfaces agree on; the library may append its live counts.
 *
 * Pure and runeless so the unit suite loads it directly.
 */

export type LibraryFilterId = 'broken' | 'remixes' | 'vocals' | 'compatible' | 'offline';

export interface LibraryFilterLabel {
	/** The visible label, exactly as the library checkbox prints it. */
	name: string;
	/** What the filter keeps or hides, in one sentence. */
	tip: string;
}

export const LIBRARY_FILTER_LABELS: Readonly<Record<LibraryFilterId, LibraryFilterLabel>> = {
	broken: {
		name: 'Broken',
		tip: 'Show broken links: include tracks whose file cannot be found (unticked hides them)'
	},
	remixes: {
		name: 'Remixes',
		tip: 'Keep only remixes: title version markers (remix / bootleg / rework / VIP / non-radio edit)'
	},
	vocals: {
		name: 'Vocals',
		tip: 'Keep only tracks with vocals: vocal regions, a vocal lyric verdict, or more than 5 lines of lyrics'
	},
	compatible: {
		name: 'compatible',
		tip: 'Show only tracks compatible with the reference deck (master, else playing, else any loaded with key and BPM)'
	},
	offline: {
		name: 'available offline',
		tip: 'Keep only tracks with local audio present (excludes cloud-only and streaming rows)'
	}
};

/** Display order, the same left-to-right order as the library's checkbox row. */
export const LIBRARY_FILTER_ORDER: readonly LibraryFilterId[] = [
	'broken',
	'remixes',
	'vocals',
	'compatible',
	'offline'
];

/** The Cmd K bar's Alt+letter shortcut for each filter (event.code KeyB, KeyR...). */
export const CMDK_FILTER_KEYS: Readonly<Record<LibraryFilterId, string>> = {
	broken: 'B',
	remixes: 'R',
	vocals: 'V',
	compatible: 'C',
	offline: 'O'
};

/** The filter an Alt+letter press toggles, or null for any other key. */
export function filterForKeyCode(code: string): LibraryFilterId | null {
	for (const id of LIBRARY_FILTER_ORDER) if (code === `Key${CMDK_FILTER_KEYS[id]}`) return id;
	return null;
}
