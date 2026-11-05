/** How long a stopped deck stays at full brightness before it dims. */
export const DECK_PAUSE_DIM_AFTER_MS = 30_000;

export interface DeckActivityInput {
	loaded: boolean;
	playing: boolean;
	audible: boolean;
	transportPending: boolean;
	commandPending: boolean;
	positionMs: number;
	durationMs: number | null;
	/** Engine loop is engaged. A stored but disengaged loop does not count. */
	loopEngaged: boolean;
	/**
	 * Wall-clock ms since this loaded deck stopped playing.
	 * Null while it is playing or empty. A deck that was loaded and never
	 * started counts as paused from the moment the UI saw it stopped.
	 */
	pausedForMs: number | null;
}

export interface DeckActivityLook {
	/** Finished, or paused at least DECK_PAUSE_DIM_AFTER_MS. */
	dim: boolean;
	/** Engaged loop: a slight orange wash, including while the deck is dim. */
	looping: boolean;
}

/**
 * Playing vs quiet for one deck panel.
 *
 * Finished matches the waveform row: playhead at or past the end, transport
 * fully stopped. An engaged loop is not finished, even at the end of the file.
 * A stop in the middle of the track waits out the pause window so a short
 * pause does not look like the deck is done.
 */
export function deckActivityLook(input: DeckActivityInput): DeckActivityLook {
	const looping = input.loaded && input.loopEngaged;
	if (!input.loaded || input.playing) return { dim: false, looping };
	const durationMs = input.durationMs;
	const finished =
		!looping &&
		durationMs !== null &&
		durationMs > 0 &&
		input.positionMs >= durationMs &&
		!input.audible &&
		!input.transportPending &&
		!input.commandPending;
	const pausedLong = input.pausedForMs !== null && input.pausedForMs >= DECK_PAUSE_DIM_AFTER_MS;
	return { dim: finished || pausedLong, looping };
}
