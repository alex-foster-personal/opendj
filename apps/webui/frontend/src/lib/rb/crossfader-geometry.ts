/**
 * Crossfader thumb geometry (MIXUX-08 AC2). Hit width drives pointer math;
 * visual width is thinner and taller inside the same hit box.
 */
export const CROSSFADER_THUMB_HIT_W = 12;
export const CROSSFADER_THUMB_VIS_W = 8;
export const CROSSFADER_THUMB_VIS_H = 18;
export const CROSSFADER_TRACK_H = 22;

export function crossfaderThumbLeftPx(value: number, trackWidth: number): number {
	return value * Math.max(0, trackWidth - CROSSFADER_THUMB_HIT_W);
}

export function crossfaderValueFromPointerX(
	clientX: number,
	rectLeft: number,
	rectWidth: number
): number {
	const x = clientX - rectLeft - CROSSFADER_THUMB_HIT_W / 2;
	return Math.min(1, Math.max(0, x / Math.max(1, rectWidth - CROSSFADER_THUMB_HIT_W)));
}
