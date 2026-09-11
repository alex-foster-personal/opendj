/** Pure gate for whether a deck row should call GET /tracks/{id}/lyrics. */

export function shouldFetchTrackLyrics(
	stableId: string,
	cachedIds: ReadonlySet<string> | null
): boolean {
	if (cachedIds === null) return false;
	return cachedIds.has(stableId);
}
