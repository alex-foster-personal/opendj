/**
 * Edge search and filters ((private path) 7.4, issue #11102 S7b).
 * Pure: no runes, no DOM. The top tab feeds in the query, the filters and the on-screen slots.
 *
 * Requirements (mini-PRD):
 *   ✔︎ matching is case-insensitive substring over name, title, cue label, description and key
 *     [if] the query is "tide" and the title is "Low Tide" [then] it matches [else ⛔️]
 *   ✔︎ broken tracks are hidden unless the Broken filter is on (library semantics)
 *     [if] a broken track and no filters [then] it does not match [else ⛔️]
 *   ✔︎ highlight ranges cover every occurrence, never overlap
 *     [if] "aa" in "aaaa" [then] [0,2] and [2,4] [else ⛔️]
 *   ✔︎ the "here" count scans only the slots on screen; the all-sets count is the server's total
 *     [if] 3 of 20 on-screen slots match and the server says 9 [then] "3 here . 9 in all sets" [else ⛔️]
 */

export interface SearchableSlot {
	title: string;
	name: string | null;
	cueLabel: string;
	description: string;
	key: string;
	broken: boolean;
	remix: boolean;
	vocals: boolean;
	offline: boolean;
	compatible: boolean;
}

/** The top tab's toggles: the library's four icon filters plus "compatible". */
export interface EdgeFilters {
	broken: boolean;
	remixes: boolean;
	vocals: boolean;
	offline: boolean;
	compatible: boolean;
}

export const NO_FILTERS: EdgeFilters = {
	broken: false,
	remixes: false,
	vocals: false,
	offline: false,
	compatible: false
};

/** Lowercase text a query is matched against. */
export function haystack(s: SearchableSlot): string {
	return [s.name ?? '', s.title, s.cueLabel, s.description, s.key].join(' ').toLowerCase();
}

/** Whether a slot survives the toggles (broken hidden unless Broken is on). */
export function passesFilters(s: SearchableSlot, f: EdgeFilters): boolean {
	if (s.broken && !f.broken) return false;
	if (f.remixes && !s.remix) return false;
	if (f.vocals && !s.vocals) return false;
	if (f.offline && !s.offline) return false;
	if (f.compatible && !s.compatible) return false;
	return true;
}

/** Whether a slot passes the toggles and contains the query. */
export function matchesSlot(s: SearchableSlot, query: string, f: EdgeFilters): boolean {
	const q = query.trim().toLowerCase();
	return passesFilters(s, f) && (!q || haystack(s).includes(q));
}

/** Whether the edge is filtering at all (a query or any toggle on). */
export function isFiltering(query: string, f: EdgeFilters): boolean {
	return query.trim() !== '' || Object.values(f).some(Boolean);
}

/** [start, end) ranges of `query` in `text`, case-insensitive. */
export function highlightRanges(text: string, query: string): Array<[number, number]> {
	const q = query.trim().toLowerCase();
	if (!q) return [];
	const t = text.toLowerCase();
	const out: Array<[number, number]> = [];
	for (let i = t.indexOf(q); i >= 0; i = t.indexOf(q, i + q.length)) out.push([i, i + q.length]);
	return out;
}

/** "n here": matches among the on-screen slots only (empty slots never match). */
export function countHere(
	onScreen: ReadonlyArray<SearchableSlot | null>,
	query: string,
	f: EdgeFilters
): number {
	return onScreen.filter((s) => s !== null && matchesSlot(s, query, f)).length;
}

/** The top tab's counts line (mock `refreshMatches`); `allSets` is the server's total for the query. */
export function countsLine(here: number, allSets: number | null): string {
	return allSets === null ? `${here} here` : `${here} here . ${allSets} in all sets`;
}
