/**
 * RESCUE-07: a /performance reload that interrupted playing decks offers a
 * one-click resume instead of landing silently stopped.
 *
 * Pure half: decides WHICH decks to offer and in what order. The reactive
 * holder (reload-resume.svelte.ts) owns the banner's lifetime and the click.
 *
 * Why a click and not an automatic start: a plain reload is not a Gig rescue
 * (RESCUE-02 is Gig-posture only), and the browser refuses to start audio on a
 * freshly loaded page until the user interacts with it. Mon 5 Oct 2026 on the
 * silver preview a hard reload mid-play left every deck stopped with AutoPlay
 * still ON, no master, and nothing on screen saying so.
 */
import type { DeckId } from '$lib/rb/deck-id';
import type { PerformanceSessionSnapshot } from '$lib/rb/performance-session-snapshot';

const DECK_IDS: readonly DeckId[] = [1, 2, 3, 4];

/** Same 10 min window as RESCUE-02 play resume: older than that, the set has moved on. */
export const RELOAD_RESUME_WINDOW_MS = 10 * 60 * 1000;

export interface ReloadResumeOffer {
	/** Decks to start, in order. The first becomes master (play-claim). */
	decks: DeckId[];
	captured_at_ms: number;
}

export function planReloadResumeOffer(input: {
	snapshot: PerformanceSessionSnapshot | null;
	/** Deck state after the session restore ran. */
	decks: Record<DeckId, { stable_id: string | null; playing: boolean }>;
	now_ms: number;
}): ReloadResumeOffer | null {
	const snapshot = input.snapshot;
	if (snapshot === null) return null;
	if (input.now_ms - snapshot.captured_at_ms > RELOAD_RESUME_WINDOW_MS) return null;
	const resumable = DECK_IDS.filter((deckId) => {
		const saved = snapshot.decks[deckId];
		const live = input.decks[deckId];
		return (
			saved.playing === true &&
			saved.stable_id !== null &&
			live.stable_id === saved.stable_id &&
			!live.playing
		);
	});
	if (resumable.length === 0) return null;
	const master = snapshot.master_deck ?? null;
	const ordered =
		master !== null && resumable.includes(master)
			? [master, ...resumable.filter((deckId) => deckId !== master)]
			: resumable;
	return { decks: ordered, captured_at_ms: snapshot.captured_at_ms };
}

/** Grep-stable console line for the offer (and the hunt collector). */
export function reloadResumeOfferMessage(offer: ReloadResumeOffer, autoPlayOn: boolean): string {
	const decks = offer.decks.join(',');
	const autoplay = autoPlayOn ? '; AutoPlay is ON and cannot arm until a deck plays' : '';
	return `[reload-resume] ${offer.decks.length} deck(s) (${decks}) were playing before reload and are stopped; click Resume to start them${autoplay}`;
}
