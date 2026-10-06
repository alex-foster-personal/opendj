/**
 * Honest stop actions for silence dropout (issue #2069). Wired from app-init so
 * master-silence-report stays free of performance-ipc cycles.
 */

import { deckStates } from '$lib/rb/audio-engine.svelte';
import { withPauseOrigin } from '$lib/rb/unexpected-pause-report';
import { recordDeckStop } from '$lib/rb/deck-stop-log';
import { cancelAutoPlayNext } from '$lib/rb/auto-play-next.svelte';
import {
	noteAutoPlaySilenceDropout
} from '$lib/rb/autoplay-silence-recover';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import type { DeckId } from '$lib/rb/deck-slots';
import type { SilenceDropoutPlan } from '$lib/rb/silence-dropout';

type SilenceDropoutStopListener = (decks: readonly DeckId[]) => void;

const _stopListeners = new Set<SilenceDropoutStopListener>();

/**
 * Hear about decks a dropout plan has just stopped (PERFMODE-15). Trackify's
 * autoplay subscribes: a watchdog stop short of the track's end is not a user
 * Pause, and an unsupervised player must move on rather than wait for input.
 * Returns the unsubscribe.
 */
export function onSilenceDropoutStopped(listener: SilenceDropoutStopListener): () => void {
	_stopListeners.add(listener);
	return () => {
		_stopListeners.delete(listener);
	};
}

async function _stopDeck(deck: DeckId): Promise<void> {
	try {
		await withPauseOrigin('dropout', () =>
			dispatchPerformanceCommand({ type: 'play', deck, playing: false }, undefined, 'engine')
		);
	} catch {
		const st = deckStates[deck];
		if (st.playing) recordDeckStop({ deck, cause: 'engine', position_ms: st.position_ms, stable_id: st.stable_id });
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
	for (const listener of [..._stopListeners]) listener(plan.stop_decks);
	if (plan.autoplay_recover) {
		noteAutoPlaySilenceDropout({ has_playable_next: true });
	}
}

export function handleSilenceDropoutPlan(plan: SilenceDropoutPlan): void {
	void executeSilenceDropoutPlan(plan);
}
