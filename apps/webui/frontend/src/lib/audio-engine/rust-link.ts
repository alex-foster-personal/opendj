/**
 * The live link to `odj-audio` that Rust engine mode's modules share: the
 * connected client, the last state frame, the load fences, and how to toast.
 * Loaded only with the mode (see `rust-mode.svelte.ts`).
 */
import { deckStates } from '$lib/player/state.svelte';
import type { DeckId } from '$lib/rb/deck-slots';

export type { DeckId };
import type { DeckState } from '$lib/rb/deck-state-types';
import type { PerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import type { AudioEngineClient, EngineCommand, EngineState } from './client';

export type RustToast = (message: string, kind: 'info' | 'error') => void;

export const link: {
	client: AudioEngineClient | null;
	lastState: EngineState | null;
	/** The page's toast, handed over by the dispatcher with each command. */
	toast: RustToast | null;
} = { client: null, lastState: null, toast: null };

/**
 * Load fence per deck. The dispatcher names the new track on the deck before
 * the engine has swapped it, so a state frame from before the swap would paint
 * the old track's playhead onto the new one. While a load is in flight the
 * value is `Infinity` (mirror nothing); once it lands it is the last frame seen,
 * and only newer frames are mirrored.
 */
export const loadFences: Partial<Record<DeckId, number>> = {};

/** Loop the anlz memory cue shows, to restore when an engine loop ends. */
export const displayLoops: Partial<Record<DeckId, DeckState['loop']>> = {};

export const DECKS = [1, 2, 3, 4] as DeckId[];

export function send(cmd: EngineCommand | PerformanceCommand): Promise<unknown> {
	if (link.client === null) throw new Error('the Rust audio engine is not connected');
	return link.client.send(cmd);
}

/** The deck's playhead: the engine's, extrapolated, while it plays. */
export function playheadMs(deck: DeckId): number {
	const st = deckStates[deck];
	if (!st.playing) return st.position_ms;
	return link.client?.positionMs(deck) ?? st.position_ms;
}

export function notify(message: string, kind: 'info' | 'error'): void {
	if (link.toast === null) throw new Error(`Rust engine mode has no toast yet for: ${message}`);
	link.toast(message, kind);
}
