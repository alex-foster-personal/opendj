import type { DeckId } from '$lib/rb/deck-id';

/** Deck menu "Load selected track" (#9448, PRRT_kwDOSEvNd860wjZw): ONE plain load, never a
 * pre-unload and never replace_playing, so the dispatcher's playing-deck guard
 * (assertDeckLoadAllowed) still refuses a load onto a sounding deck. Null with no selection. */
export function deckLoadSelectionCommand(
	deck: DeckId,
	selectedRow: string | null
): { type: 'load'; deck: DeckId; stable_id: string } | null {
	return selectedRow === null ? null : { type: 'load', deck, stable_id: selectedRow };
}

/** Deck menu Track info: deck-targeted, so it opens THIS deck's record (PRRT_kwDOSEvNd860v-hu). */
export function deckTrackInfoCommand(deck: DeckId): { type: 'track_info_open'; deck: DeckId } {
	return { type: 'track_info_open', deck };
}

/** Move-to-N labels from deckMoveMenuEntries (PRRT_kwDOSEvNd860v-ho): id `move-to-${to}` -> label. */
export function deckMoveLabels(
	entries: readonly { to: DeckId; label: string }[]
): Map<string, string> {
	const labels = new Map<string, string>();
	for (const { to, label } of entries) {
		labels.set(`move-to-${to}`, label);
	}
	return labels;
}

/** Relabel registry items whose id has a move label; other items pass through unchanged. */
export function applyDeckMenuLabels<T extends { id: string; label: string }>(
	items: readonly T[],
	labels: ReadonlyMap<string, string>
): T[] {
	return items.map((item) => {
		const override = labels.get(item.id);
		return override === undefined ? item : { ...item, label: override };
	});
}
