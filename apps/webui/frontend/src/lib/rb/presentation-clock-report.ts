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

import { noteOutputStall } from '$lib/rb/audio-output-rebind';
import { recordPerfEvent } from '$lib/rb/perf-event-log';
import { pushToast } from '$lib/stores.svelte';

/**
 * The deck id, declared inline rather than imported.
 *
 * Deliberate, and the same call perf-event-log.ts makes for the same reason:
 * `deck-slots.ts` is the tree's fan-in ceiling and the quality ratchet holds
 * that key at its measured floor with ZERO headroom on purpose, so the next
 * importer reds the gate for every lane at once. A four-member union is not
 * worth doing that to whoever rebases next.
 */
type DeckId = 1 | 2 | 3 | 4;

/** Per-deck: was the output clock stalled on the previous observation. */
const _stalled: Record<DeckId, boolean> = { 1: false, 2: false, 3: false, 4: false };

/** Live view for the UI, so a deck can show the operator that it is coasting. */
export function isPresentationClockStalled(deck: DeckId): boolean {
	return _stalled[deck];
}

/**
 * Clear every deck's stall latch, for a fresh audio graph.
 *
 * `_stalled` is module-level and outlives any one graph, so a deck slot torn
 * down mid-stall would otherwise leave a stale `true` for the NEXT session to
 * inherit before that slot is re-observed (P2 review thread on PR #1693).
 * Called from `_ensureGraph()` alongside `resetMasterSilenceWatch()`, the
 * same "new graph, new state" moment.
 */
export function resetPresentationClockStall(): void {
	for (const deck of [1, 2, 3, 4] as const satisfies readonly DeckId[]) _stalled[deck] = false;
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
		// A stalled device position is also the only signal Chromium gives when the
		// output device changed under a running context; ask for a re-bind.
		noteOutputStall(deck);
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

/**
 * Report a presentation frame that threw.
 *
 * Lives here rather than inline in `_tick` purely for the engine's line budget:
 * `audio-engine.svelte.ts` is AT the ratchet's `file_size.max_frontend` cap, so
 * the catch block is a call rather than a paragraph.
 */
export function notePresentationTickFailure(error: unknown): void {
	recordPerfEvent(
		'presentation-tick-failed',
		'the presentation frame threw, so the waveform would have frozen over live audio ' +
			`while the decks kept playing: ${String(error)}`,
		null,
		'error'
	);
}

/**
 * The raw output timestamp, validated.
 *
 * Moved out of `audio-engine.svelte.ts` with the tick guard, for the engine's
 * line budget (it is AT the ratchet's `file_size.max_frontend` cap). Both
 * throws are deliberate and unchanged: a browser with no `getOutputTimestamp`
 * cannot drive presented transport at all, and a partial timestamp is not
 * something to guess at. They are now caught by `_tick`, recorded, and the next
 * frame is re-armed - which is what makes them safe to keep throwing.
 */
export function readOutputTimestamp(context: AudioContext): {
	contextTime: number;
	performanceTime: number;
} {
	if (typeof context.getOutputTimestamp !== 'function') {
		throw new Error('AudioContext.getOutputTimestamp is required for presented transport');
	}
	const { contextTime, performanceTime } = context.getOutputTimestamp();
	if (contextTime === undefined || performanceTime === undefined) {
		throw new Error('AudioContext.getOutputTimestamp returned an incomplete timestamp');
	}
	return { contextTime, performanceTime };
}

/**
 * When each deck's published `position_ms` was sampled (`performance.now()`).
 *
 * Painting projects a playing deck's position forward between samples. It
 * must project from the instant the engine took the sample, not from the frame
 * that first noticed it: rows notice a tick in different frames, so anchoring
 * on notice painted synced decks a frame apart (see `paint-position.ts`).
 * Written by both engines' per-frame publish; a position written any other way
 * (seek, unload) holds a different value, so `positionSampledAtMs` says null.
 */
const _positionSamples: Record<DeckId, { positionMs: number; atMs: number } | null> = { 1: null, 2: null, 3: null, 4: null };

export function notePositionSample(deck: DeckId, positionMs: number, atMs: number): void {
	_positionSamples[deck] = { positionMs, atMs };
}

/** The sample time of `positionMs` on `deck`, or null when it is not the latest sample. */
export function positionSampledAtMs(deck: DeckId, positionMs: number): number | null {
	const sample = _positionSamples[deck];
	return sample !== null && sample.positionMs === positionMs ? sample.atMs : null;
}
