/**
 * Which rows get the gross key-clash edge (LIBUX-8682).
 *
 * The set summary (server, `summarize_set`) lists clashes between adjacent
 * NON-skipped tracks in playlist order. The table draws a clash on the top
 * border of the `to` row's Key and BPM cells, but only when the row shown
 * just above it, skipped rows bridged, is the clash's `from` track, so a
 * sorted or filtered view never draws a line between tracks that are not
 * neighbours on screen.
 */

import type { KeyClash } from './set-target';

export function clashTitle(clash: KeyClash): string {
	return `Key clash ${clash.from_key} -> ${clash.to_key} (${clash.steps} step${clash.steps === 1 ? '' : 's'})`;
}

/** stable_id of the lower row -> hover title, for every gross clash whose two tracks are on-screen neighbours. */
export function grossClashEdges(
	shownIds: readonly string[],
	skipIds: ReadonlySet<string>,
	clashes: readonly KeyClash[]
): Map<string, string> {
	const byPair = new Map<string, KeyClash>();
	for (const clash of clashes) if (clash.gross) byPair.set(`${clash.from_id}>${clash.to_id}`, clash);
	const edges = new Map<string, string>();
	if (byPair.size === 0) return edges;
	let previous: string | null = null;
	for (const id of shownIds) {
		if (skipIds.has(id)) continue;
		const clash = previous === null ? undefined : byPair.get(`${previous}>${id}`);
		if (clash !== undefined) edges.set(id, clashTitle(clash));
		previous = id;
	}
	return edges;
}
