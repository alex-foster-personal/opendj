/**
 * Honest stop actions for silence dropout (issue #2069). Wired from app-init so
 * master-silence-report stays free of performance-ipc cycles.
 */

import { deckStates } from '$lib/rb/audio-engine.svelte';
import { withPauseOrigin } from '$lib/rb/unexpected-pause-report';
import { cancelAutoPlayNext } from '$lib/rb/auto-play-next.svelte';
import {
	noteAutoPlaySilenceDropout
} from '$lib/rb/autoplay-silence-recover';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import type { DeckId } from '$lib/rb/deck-slots';
import type { SilenceDropoutPlan } from '$lib/rb/silence-dropout';

async function _stopDeck(deck: DeckId): Promise<void> {
	try {
		await withPauseOrigin('dropout', () =>
			dispatchPerformanceCommand({ type: 'play', deck, playing: false })
		);
	} catch {
		const st = deckStates[deck];
		st.playing = false;
		st.audible = false;
		st.transport_pending = false;
	}
}

export async function executeSilenceDropoutPlan(plan: SilenceDropoutPlan): Promise<void> {
	cancelAutoPlayNext();
	for (const deck of plan.stop_decks) {
		await _stopDeck(deck);
	}
	if (plan.autoplay_recover) {
		noteAutoPlaySilenceDropout({ has_playable_next: true });
	}
}

export function handleSilenceDropoutPlan(plan: SilenceDropoutPlan): void {
	void executeSilenceDropoutPlan(plan);
}
