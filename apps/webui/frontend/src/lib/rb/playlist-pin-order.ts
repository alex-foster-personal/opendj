/**
 * Pin + "new at top" ordering for playlist lists (Spotify rail + collection tree).
 * Pinned IDs keep their pin order; optional newIds float just below pins.
 */

export function orderByPinsAndNew<T>(
	items: readonly T[],
	idOf: (item: T) => string,
	pinnedIds: readonly string[],
	newIds: ReadonlySet<string> = new Set()
): T[] {
	const pinRank = new Map(pinnedIds.map((id, i) => [id, i] as const));
	const indexed = items.map((item, apiIndex) => ({ item, apiIndex, id: idOf(item) }));
	indexed.sort((a, b) => {
		const pa = pinRank.get(a.id);
		const pb = pinRank.get(b.id);
		const aPinned = pa !== undefined;
		const bPinned = pb !== undefined;
		if (aPinned && bPinned) return (pa as number) - (pb as number);
		if (aPinned) return -1;
		if (bPinned) return 1;
		const aNew = newIds.has(a.id);
		const bNew = newIds.has(b.id);
		if (aNew !== bNew) return aNew ? -1 : 1;
		return a.apiIndex - b.apiIndex;
	});
	return indexed.map((row) => row.item);
}

export function pinRankOf(id: string, pinnedIds: readonly string[]): number | null {
	const i = pinnedIds.indexOf(id);
	return i < 0 ? null : i;
}
