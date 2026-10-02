/**
 * Record unexpected playing-to-paused transitions (issue #2153).
 */

import { recordPerfEvent } from '$lib/rb/perf-event-log';
import { pushToast, TOAST_DEFAULT_MS } from '$lib/stores.svelte';
import {
	diagnoseUnexpectedPause,
	formatUnexpectedPauseDiagnostic,
	formatUnexpectedPauseMessage,
	type PauseOrigin,
	type UnexpectedPauseCause
} from '$lib/rb/unexpected-pause';
import type { DeckId } from '$lib/rb/deck-slots';

type PlayingPosition = {
	deck: DeckId;
	position_ms: number;
	decoded_duration_ms?: number | null;
	metadata_duration_ms?: number | null;
};

let _pauseOrigin: PauseOrigin = 'other';
let _playingPositionReader: (() => readonly PlayingPosition[]) | null = null;
let _autoplayReader: () => { armed: boolean; handoff_in_flight: boolean } = () => ({
	armed: false,
	handoff_in_flight: false
});

export function withPauseOrigin<T>(origin: PauseOrigin, fn: () => T): T {
	const prev = _pauseOrigin;
	_pauseOrigin = origin;
	try {
		return fn();
	} finally {
		_pauseOrigin = prev;
	}
}

export function readPauseOrigin(): PauseOrigin {
	return _pauseOrigin;
}

export function setPlayingPositionReader(
	reader: (() => readonly PlayingPosition[]) | null
): void {
	_playingPositionReader = reader;
}

export function readPlayingPositions(): readonly PlayingPosition[] {
	return _playingPositionReader?.() ?? [];
}

export function setUnexpectedPauseAutoPlayReader(
	reader: (() => { armed: boolean; handoff_in_flight: boolean }) | null
): void {
	_autoplayReader = reader ?? (() => ({ armed: false, handoff_in_flight: false }));
}

export function recordUnexpectedPause(input: {
	cause: UnexpectedPauseCause;
	deck: DeckId;
	position_ms: number;
	context_state?: string;
	decoded_duration_ms?: number | null;
	metadata_duration_ms?: number | null;
	cause_error?: unknown;
}): void {
	const diagnostic = formatUnexpectedPauseDiagnostic(input);
	const human = formatUnexpectedPauseMessage(input);
	recordPerfEvent('audio-unexpected-pause', diagnostic, input.deck, 'error');
	if (input.cause !== 'context-suspended') {
		pushToast(human, 'error', TOAST_DEFAULT_MS, input.cause_error, {
			source: 'unexpected-pause',
			cause: input.cause,
			deck: input.deck
		});
	}
}

/**
 * A stop request is a falling edge only when the deck's standing intent was to
 * play. A schedule that carries `active: false` onto a deck that was already
 * paused (a seek, cue jump or sync re-anchor landing while a pause is still in
 * flight) re-states the pause; it is not a new stop. Classifying it anyway read
 * the untagged origin plus the pre-pause position as a track cut short, and
 * raised a false "stopped before the decoded audio ends" toast.
 */
export function notePlayingFallingEdge(input: {
	was_active: boolean;
	origin: PauseOrigin;
	deck: DeckId;
	position_ms: number;
	duration_ms: number | null;
	metadata_duration_ms?: number | null;
	processor_error: string | null;
	context_state: string;
}): void {
	if (!input.was_active) return;
	const autoplay = _autoplayReader();
	const cause = diagnoseUnexpectedPause({
		origin: input.origin,
		autoplay_armed: autoplay.armed,
		context_state: input.context_state,
		playing: false,
		position_ms: input.position_ms,
		duration_ms: input.duration_ms,
		processor_error: input.processor_error,
		autoplay_handoff_in_flight: autoplay.handoff_in_flight
	});
	if (cause === null) return;
	recordUnexpectedPause({
		cause,
		deck: input.deck,
		position_ms: input.position_ms,
		context_state: input.context_state,
		decoded_duration_ms: input.duration_ms,
		metadata_duration_ms: input.metadata_duration_ms ?? null
	});
}
