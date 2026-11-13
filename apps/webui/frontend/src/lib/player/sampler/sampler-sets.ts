/**
 * Sets: pages of slots shown on one edge ((private path) 7.4).
 * Pure: generic over the slot type, so the store and the tests share it.
 *
 * Requirements (mini-PRD):
 *   ✔︎ slots are split into sets of `perSet`, in catalogue order
 *     [if] 45 slots and perSet 20 [then] sets of 20, 20, 5 [else ⛔️]
 *   ✔︎ inserting into a full set never loses a slot: the last one moves to the next set
 *     [if] a capture is dropped on slot 3 of a full set [then] the old last slot is first in the next set [else ⛔️]
 *   ✔︎ inserting on an empty slot fills it and moves nothing
 *     [if] a capture is dropped on an empty slot [then] only that slot changes [else ⛔️]
 */

export interface SampleSet<T> {
	id: string;
	name: string;
	description: string;
	items: Array<T | null>;
}

/** Split `ordered` into sets of `perSet`, ids `set-1`, `set-2`, ..., names from `meta` if given. */
export function buildSets<T>(
	ordered: readonly T[],
	perSet: number,
	meta: Record<string, { name: string; description: string }> = {}
): SampleSet<T>[] {
	if (!(perSet > 0)) throw new Error(`buildSets: perSet must be positive, got ${perSet}`);
	const sets: SampleSet<T>[] = [];
	for (let i = 0; i < ordered.length; i += perSet) {
		const id = `set-${sets.length + 1}`;
		sets.push({
			id,
			name: meta[id]?.name ?? `Set ${sets.length + 1}`,
			description: meta[id]?.description ?? '',
			items: ordered.slice(i, i + perSet)
		});
	}
	return sets;
}

/** Insert `item` at `index` of set `setIndex` in place; a full set pushes its last slot on. */
export function insertIntoSet<T>(
	sets: SampleSet<T>[],
	setIndex: number,
	index: number,
	item: T,
	perSet: number
): void {
	if (setIndex >= sets.length) {
		const id = `set-${sets.length + 1}`;
		sets.push({ id, name: `Set ${sets.length + 1}`, description: '', items: [item] });
		return;
	}
	const items = sets[setIndex].items;
	if (index < items.length && items[index] === null) {
		items[index] = item;
		return;
	}
	items.splice(Math.min(index, items.length), 0, item);
	const hole = items.indexOf(null, index + 1);
	if (hole >= 0) items.splice(hole, 1);
	else if (items.length > perSet) {
		const overflow = items.pop() as T;
		insertIntoSet(sets, setIndex + 1, 0, overflow, perSet);
	}
}

/** Set index after paging `delta` from `current`, wrapping both ways; 0 when there are none. */
export function pageWrap(current: number, delta: number, count: number): number {
	return count === 0 ? 0 : (((current + delta) % count) + count) % count;
}
