/**
 * AGENT-8332/54: what an agent order reports when a load is refused because
 * the deck is still sounding, and what a `replace_playing` load stopped.
 *
 * Pure: it reads the refusal and a performance-state snapshot the caller
 * passes in, so the step document an agent receives is unit-testable without
 * an audio engine. The guard itself lives in `audio-engine-guards.ts`.
 */
import type { DeckId } from '$lib/player/constants';
import { DeckReplacementBlockedError } from './audio-engine-guards';

/** The deck fields this module reads from `queryPerformanceState().decks[deck]`. */
export interface RefusalDeckView {
	stable_id: string | null;
	title: string | null;
	artist: string | null;
	position_ms: number;
	playing: boolean;
	audible: boolean;
}

export interface RefusalStateView {
	decks: Record<DeckId, RefusalDeckView>;
}

export interface PlayingDeckSnapshot {
	deck: DeckId;
	stable_id: string | null;
	title: string | null;
	artist: string | null;
	position_ms: number;
}

export interface DeckPlayingRefusal {
	code: 'deck_playing';
	detail: PlayingDeckSnapshot & {
		/** The guard's own reasons, e.g. `playing, audible, controlActive`. */
		active_state: string[];
		/** The command to send to replace the playing track anyway, or null when
		 * the refused command has no override (only `load` carries one). */
		override: Record<string, unknown> | null;
	};
}

function _snapshot(deck: DeckId, state: RefusalStateView): PlayingDeckSnapshot {
	const view = state.decks[deck];
	return {
		deck,
		stable_id: view.stable_id,
		title: view.title,
		artist: view.artist,
		position_ms: Math.round(view.position_ms)
	};
}

function _record(command: unknown): Record<string, unknown> | null {
	return command !== null && typeof command === 'object' && !Array.isArray(command)
		? (command as Record<string, unknown>)
		: null;
}

/** The structured refusal for a step, or null when `error` is not the playing-deck guard. */
export function deckPlayingRefusal(
	error: unknown,
	command: unknown,
	state: RefusalStateView
): DeckPlayingRefusal | null {
	if (!(error instanceof DeckReplacementBlockedError)) return null;
	const record = _record(command);
	const override = record !== null && record.type === 'load' ? { ...record, replace_playing: true } : null;
	return {
		code: error.code,
		detail: { ..._snapshot(error.deck, state), active_state: [...error.activeState], override }
	};
}

/** The track a `replace_playing` load is about to stop, or null when it stops nothing. */
export function playingDeckReplaced(
	command: Record<string, unknown>,
	state: RefusalStateView
): PlayingDeckSnapshot | null {
	if (command.type !== 'load' || command.replace_playing !== true) return null;
	const deck = command.deck as DeckId;
	const view = state.decks[deck];
	if (view === undefined || !(view.playing || view.audible)) return null;
	return _snapshot(deck, state);
}
