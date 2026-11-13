/**
 * The selector's one polite status announcement (#12253).
 *
 * Only a real selection change speaks: a move or drill that keeps the same node (no eligible
 * neighbour) says nothing, and a clear says "Selection cleared" once, so a screen reader user
 * hears that W A S D left the controls instead of silence that reads like a dropped key.
 * The registry holds no human label per node today, so callers pass `null` and the node id is
 * spoken as words (#14068): `deck.1.cues` says "Deck 1, Hot cues". An explicit label wins.
 */
import { selectorSpokenName } from './selector-spoken-name';

export const SELECTOR_CLEARED_ANNOUNCEMENT = 'Selection cleared';

/** The message for a selection going from `previousId` to `nextId`, or null when nothing changed. */
export function selectorAnnouncement(
	previousId: string | null,
	nextId: string | null,
	label: string | null
): string | null {
	if (previousId === nextId) return null;
	if (nextId === null) return SELECTOR_CLEARED_ANNOUNCEMENT;
	return selectorSpokenName(nextId, label);
}

export interface SelectorAnnouncer {
	/** Record the selection now shown (null when cleared); emits only when it really changed. */
	track(nextId: string | null, label: string | null): void;
}

/** Remembers the last announced selection and calls `emit` once per real change. */
export function createSelectorAnnouncer(emit: (message: string) => void): SelectorAnnouncer {
	let previousId: string | null = null;
	return {
		track(nextId, label): void {
			const message = selectorAnnouncement(previousId, nextId, label);
			previousId = nextId;
			if (message !== null) emit(message);
		}
	};
}
