/** AGENT-51 transition-in runner extracted for unit tests (issue #8453). */
import type { DeckId } from '$lib/rb/deck-id';

export async function runTransitionInCue(
	click: { stableId: string; timeS: number },
	deps: {
		setTransitionInCue: (stableId: string, inMs: number) => Promise<{ bar: number }>;
		pushToast: (message: string, kind: 'info' | 'warn' | 'error') => void;
		deckIds: readonly DeckId[];
		getDeckState: (id: DeckId) => { stable_id: string | null };
		refreshHotCues: (id: DeckId) => Promise<void>;
	}
): Promise<void> {
	try {
		const set = await deps.setTransitionInCue(click.stableId, click.timeS * 1000);
		deps.pushToast(`Transition-in cue set at bar ${set.bar}`, 'info');
		try {
			for (const id of deps.deckIds) {
				if (deps.getDeckState(id).stable_id === click.stableId) {
					await deps.refreshHotCues(id);
				}
			}
		} catch (refreshErr) {
			deps.pushToast(
				`Transition-in cue saved but waveform did not refresh: ${refreshErr instanceof Error ? refreshErr.message : String(refreshErr)}`,
				'error'
			);
		}
	} catch (e) {
		deps.pushToast(`Transition-in cue not set: ${e instanceof Error ? e.message : String(e)}`, 'error');
	}
}
