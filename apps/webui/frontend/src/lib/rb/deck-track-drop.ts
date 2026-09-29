import type { DeckId } from './deck-slots';
import { noteRecentDeck } from '$lib/rb/recent-deck';
import { trackDragRefusal, type DraggableRow } from './track-drag-refusal';

type DeckLoadCommand =
	| { type: 'unload'; deck: DeckId }
	| { type: 'load'; deck: DeckId; stable_id: string };

export async function applyDeckTrackDrop(args: {
	deckId: DeckId;
	occupied: boolean;
	stableId: string;
	row: DraggableRow | null;
	dispatch: (command: DeckLoadCommand) => Promise<unknown>;
	toast: (message: string, kind: 'error') => void;
}): Promise<void> {
	if (args.row !== null) {
		const why = trackDragRefusal(args.row);
		if (why !== null) {
			args.toast(why, 'error');
			return;
		}
	}
	try {
		noteRecentDeck(args.deckId);
		if (args.occupied) {
			await args.dispatch({ type: 'unload', deck: args.deckId });
		}
		await args.dispatch({ type: 'load', deck: args.deckId, stable_id: args.stableId });
	} catch (error: unknown) {
		const message = error instanceof Error ? error.message : String(error);
		args.toast(`drop load failed: ${message}`, 'error');
	}
}
