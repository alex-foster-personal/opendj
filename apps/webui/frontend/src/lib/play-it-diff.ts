/** Pure helpers for summarising a PLAY IT solve result (play-it-sort-action). */

export interface ReorderSummary {
	movedCount: number;
	unchanged: boolean;
}

/** How many stable_ids landed at a different position between the two
 * orderings. Assumes both are permutations of the same set (server-checked
 * by the solver, which only ever reorders the playlist's existing items). */
export function summarizeReorder(previous: string[], proposed: string[]): ReorderSummary {
	const moved = proposed.reduce(
		(count, id, i) => count + (previous[i] === id ? 0 : 1),
		0
	);
	return { movedCount: moved, unchanged: moved === 0 };
}
