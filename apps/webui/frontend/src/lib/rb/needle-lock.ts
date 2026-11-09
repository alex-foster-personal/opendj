/**
 * Needle Lock (DECKCTL-10421, rekordbox Preferences > Needle Lock, mimicked):
 * while a deck plays, a click on its overview strip does not seek, so a stray
 * touch cannot jump a live track. A paused deck still seeks. The main
 * scrolling waveform and keyboard jumps are not affected.
 */
export const NEEDLE_LOCK_HINT = 'Needle Lock is on: pause the deck to seek from the overview';

/** The hint to show instead of seeking, or null when the strip click may seek. */
export function needleLockRefusal(needleLock: boolean, deckPlaying: boolean): string | null {
	return needleLock && deckPlaying ? NEEDLE_LOCK_HINT : null;
}
