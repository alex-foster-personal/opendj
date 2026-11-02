/**
 * SETNOTE-6340d/09: what following a note link does. While /performance is
 * mounted (Gig) a track loads onto a deck - deck 1 for a Cmd-click, the
 * pressed one for hover + 1-4 - through `window.musicDjToolsPerformance`, the
 * IPC bridge into the shared typed dispatcher (stem links with stems on).
 * Elsewhere there is no engine to load into, so it opens the track page. Web
 * links open in a new tab.
 */
import { goto } from '$app/navigation';
import type { DeckId } from '$lib/rb/deck-id';
import { FIRST_DECK, type NoteTarget } from './editor';

export const inGig = (): boolean => window.musicDjToolsPerformance !== undefined;

export async function openNoteTarget(target: NoteTarget, deck: DeckId = FIRST_DECK): Promise<void> {
	if (target.kind === 'http') {
		window.open(target.url, '_blank', 'noopener');
		return;
	}
	const ipc = window.musicDjToolsPerformance;
	if (ipc === undefined) {
		await goto(`/track/${encodeURIComponent(target.stableId)}`);
		return;
	}
	await ipc.dispatch({
		type: 'load',
		deck,
		stable_id: target.stableId,
		stems: target.kind === 'stem'
	});
}
