/**
 * Press-to-value geometry shared by the live mixer fader (VFader.svelte) and the Settings drag
 * test fader (DragTestFader.svelte), so a press away from the thumb jumps it to the same value
 * on both. The thumb's centre sits under the pointer: the track's usable travel is its height
 * minus one thumb, top is 1 and bottom is 0, clamped at both ends.
 */

/** Thumb height in px for both faders. */
export const FADER_THUMB_H = 12;

export interface FaderTrackRect {
	top: number;
	height: number;
}

/** The 0..1 value a press at `clientY` on a track at `rect` sets. */
export function faderValueFromPress(
	rect: FaderTrackRect,
	clientY: number,
	thumbH: number = FADER_THUMB_H
): number {
	const y = clientY - rect.top - thumbH / 2;
	return Math.min(1, Math.max(0, 1 - y / Math.max(1, rect.height - thumbH)));
}
