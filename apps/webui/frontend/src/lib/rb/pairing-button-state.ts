/**
 * Create pairing button state (DECKUX-12): why the top-bar button is
 * disabled. Separate from pairing-readiness.ts on purpose: that module rides
 * the library first paint with the performance dispatcher, while this one is
 * only needed by the /performance top bar (bundle budget, PR #4014).
 */

interface PairingDeckReadiness {
	deckId: number;
	loaded: boolean;
	beatCount: number;
}

/**
 * Why Create pairing cannot open right now, or null when it can. A pairing
 * links two loaded decks, and with BeatSyncMax on each deck's position is
 * recorded as a beat, so a loaded deck with no beatgrid at all cannot be
 * captured. The button shows this as its disabled tooltip instead of failing
 * after the click.
 */
export function pairingUnavailableReason(
	decks: readonly PairingDeckReadiness[],
	beatSyncMax: boolean
): string | null {
	const loaded = decks.filter((deck) => deck.loaded);
	if (loaded.length < 2) return 'Load a track on two decks to create a pairing';
	if (!beatSyncMax) return null;
	const gridless = loaded.filter((deck) => deck.beatCount === 0).map((deck) => `CH${deck.deckId}`);
	if (gridless.length === 0) return null;
	const verb = gridless.length === 1 ? 'has' : 'have';
	return (
		`${gridless.join(' and ')} ${verb} no beatgrid yet, and BeatSyncMax records pairing positions ` +
		'in beats. Wait for analysis, or turn BeatSyncMax off to record time instead'
	);
}
