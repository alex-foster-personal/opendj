/** ONE beat-jump display formatter (DECKUX-10470): magnitude labels for the
 * size readout and signed deltas for jump titles. Pure, so unit tests load it
 * without Svelte. */

/** Maps jump size to the on-deck readout (0.5 -> `1/2`, else decimal string). */
export function formatBeatJumpMagnitude(size: number): string {
	return size === 0.5 ? '1/2' : String(size);
}

/** Maps transport jump delta to title text (`+4`, `-1/2`, …). */
export function formatBeatJumpDelta(delta: number): string {
	const magnitude = formatBeatJumpMagnitude(Math.abs(delta));
	return delta > 0 ? `+${magnitude}` : `-${magnitude}`;
}

/** Legacy export name used by BeatJump and beat-jump-size tests. */
export function beatJumpSizeLabel(size: number): string {
	return formatBeatJumpMagnitude(size);
}
