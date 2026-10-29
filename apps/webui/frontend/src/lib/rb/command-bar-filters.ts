/**
 * CMDK-05: the Cmd K bar applies the library's own filter toggles.
 *
 * Same prefs (uiPrefs), same row rules as the library view, so flipping a
 * filter in either place changes both and a track the library hides is hidden
 * here too. The rules are the library's: rowRendersUnavailable for Broken,
 * is_remix for Remixes, the three-way vocals verdict for Vocals (an unmeasured
 * row is excluded, as in the library), local audio for available offline, and
 * isAppropriateNext against the reference deck for compatible.
 *
 * Runeless so the unit suite loads it directly.
 */
import { rowRendersUnavailable } from '$lib/components/rb/browser/browser-row-wire';
import { rowVocalsVerdict } from '$lib/components/rb/browser/vocals-filter';
import type { LibraryFilterId } from './library-filter-labels';
import { isAppropriateNext, type NextOnlyRef } from './next-only-filter';

/** The uiPrefs fields the filters read. */
export interface CmdkFilterPrefs {
	hide_broken_links: boolean;
	remixes_filter: boolean;
	vocals_filter: boolean;
	next_only_filter: boolean;
	available_offline_filter: boolean;
	compatible_filter: NonNullable<Parameters<typeof isAppropriateNext>[2]>;
}

/** The row fields the filters read; index rows and pane rows both fit. */
export interface CmdkFilterRow {
	stable_id: string;
	key: string | null;
	bpm: number | null;
	file_exists: boolean | null;
	file_availability?: string | null;
	is_streaming?: boolean | null;
	is_remote?: boolean | null;
	spotify_pending?: boolean | null;
	file_path?: string | null;
	is_remix?: boolean | null;
	vocals?: Parameters<typeof rowVocalsVerdict>[0]['vocals'];
	lyrics?: Parameters<typeof rowVocalsVerdict>[0]['lyrics'];
}

/** Whether each filter is ON. Broken is on when broken rows SHOW (the library's checkbox reads that way). */
export function filterStates(prefs: CmdkFilterPrefs): Record<LibraryFilterId, boolean> {
	return {
		broken: !prefs.hide_broken_links,
		remixes: prefs.remixes_filter,
		vocals: prefs.vocals_filter,
		compatible: prefs.next_only_filter,
		offline: prefs.available_offline_filter
	};
}

function locallyAvailable(row: CmdkFilterRow): boolean {
	if (row.is_streaming === true || row.spotify_pending === true) return false;
	return row.file_exists === true;
}

/**
 * The compatible filter's rules with shifts off. The filter is only read when it
 * is on, so a missing object fails here rather than falling back to defaults that
 * would change what Cmd-K keeps.
 */
function unshiftedCompatible(prefs: CmdkFilterPrefs): Parameters<typeof isAppropriateNext>[2] {
	if (prefs.compatible_filter === undefined || prefs.compatible_filter === null) {
		throw new Error('cmdkKeep: next_only_filter is on but prefs.compatible_filter is missing');
	}
	return { ...prefs.compatible_filter, key_shift_max: 0 as const };
}

/**
 * The keep-predicate for the active filters. `ref` is the compatible filter's
 * reference deck (computeNextOnlyRef); with no reference the compatible filter
 * keeps every row, exactly as the library's _applyNextOnly does.
 * `hideBroken` is the broken filter as the open pane resolved it: the browser
 * exempts the Missing Tracks view (hideBrokenForActivePane), so the bar must
 * not hide the rows that pane exists to show. It defaults to the preference.
 */
export function cmdkKeep(
	prefs: CmdkFilterPrefs,
	ref: NextOnlyRef | null,
	hideBroken: boolean = prefs.hide_broken_links
): (row: CmdkFilterRow) => boolean {
	return (row) => {
		if (hideBroken && rowRendersUnavailable(row)) return false;
		if (prefs.remixes_filter && row.is_remix !== true) return false;
		if (
			prefs.vocals_filter &&
			rowVocalsVerdict({ vocals: row.vocals ?? { status: 'not_analyzed' }, lyrics: row.lyrics ?? null }) !== 'yes'
		) {
			return false;
		}
		if (prefs.available_offline_filter && !locallyAvailable(row)) return false;
		// KEYSHIFT-03: Cmd-K shows no shift superscript and loads unshifted, so
		// it keeps only rows compatible without a key shift.
		if (prefs.next_only_filter && ref !== null && !isAppropriateNext(row, ref, unshiftedCompatible(prefs))) {
			return false;
		}
		return true;
	};
}
