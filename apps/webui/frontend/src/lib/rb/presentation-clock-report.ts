/**
 * Reporting for a stalled HAL output position, kept out of the engine.
 *
 * `audio-engine.svelte.ts` is the largest hand-written frontend module and sits
 * near the quality ratchet's `file_size.max_frontend` cap, so instrumentation
 * lives beside it rather than in it - the same convention (D5,
 * docs/perf/performance-register.md) that produced
 * `audio-context-instrumentation.ts` and `press-stamp.ts`.
 *
 * What it owns is the EDGE: `observePresentedTransportTimeline` returns a
 * verdict every frame, at ~60Hz per deck, and a toast or a perf row per frame
 * would be 3,600 rows a minute describing one continuous condition. Only the
 * transitions are reported.
 */

import { recordPerfEvent } from '$lib/rb/perf-event-log';
import type { DeckId } from '$lib/rb/deck-slots';
import { pushToast } from '$lib/stores.svelte';

/** Per-deck: was the output clock stalled on the previous observation. */
const _stalled: Record<DeckId, boolean> = { 1: false, 2: false, 3: false, 4: false };

/** Live view for the UI, so a deck can show the operator that it is coasting. */
export function isPresentationClockStalled(deck: DeckId): boolean {
	return _stalled[deck];
}

/**
 * Record a transition in the output clock's health for one deck.
 *
 * `null` means the stall could not be measured this frame (an unreadable
 * `performanceTime`). That is NOT evidence of recovery, so it leaves the
 * previous verdict standing rather than silently clearing an active stall.
 */
export function notePresentationClock(deck: DeckId, clockStalled: boolean | null): void {
	if (clockStalled === null) return;
	if (clockStalled === _stalled[deck]) return;
	_stalled[deck] = clockStalled;
	if (clockStalled) {
		recordPerfEvent(
			'presentation-clock-stalled',
			`deck ${deck}: the audio device's reported output position stopped advancing, so ` +
				'the waveform is running on the render clock and now LEADS what you can hear ' +
				'by roughly the output latency',
			deck,
			'error'
		);
		pushToast(`Deck ${deck}: audio device clock stalled - waveform is estimated`, 'error');
		return;
	}
	recordPerfEvent(
		'presentation-clock-recovered',
		`deck ${deck}: the audio device's output position is advancing again; the waveform ` +
			'is back on device truth',
		deck,
		'info'
	);
}
