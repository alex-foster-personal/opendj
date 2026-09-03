/**
 * Where the pitch fader's thumb sits, and what a pointer position means.
 *
 * Pure, and split out of PitchFader.svelte, because both halves were wrong in
 * the same way and only a test comparing them could show it (pin
 * ebb1def0234c: "are all pitch sliders currently off-zero by default?").
 *
 * They were not off-zero - they were DRAWN off-zero. The component hardcoded a
 * 96px track "matching .rb-fader in theme.css", which is `height: 100%` with a
 * `min-height: 64px`, so the real track is whatever the deck layout gives it.
 * The render used the constant; the pointer mapping measured the element and
 * then divided by the constant anyway. So 0% drew above centre on any track
 * taller than 96px, and dragging to the visual centre did not produce 0%.
 *
 * Both directions now take the measured height, and the round trip between
 * them is what the test asserts.
 */

/** Thumb height, matching `.rb-fader-thumb` in theme.css. This one is a real
 * fixed px value in the stylesheet, unlike the track's. */
export const THUMB_H = 12;

/** Travel available to the thumb's top edge. Never negative. */
function _travel(trackH: number): number {
	return Math.max(0, trackH - THUMB_H);
}

/**
 * Top offset in px for a fader value (0 = bottom / -range, 1 = top / +range).
 * 0.5 is 0% pitch and must land dead centre at every track height.
 */
export function thumbOffsetPx(value: number, trackH: number): number {
	return (1 - value) * _travel(trackH);
}

/** Fader value for a pointer offset measured from the track's top edge. */
export function valueFromPointer(offsetPx: number, trackH: number): number {
	const travel = _travel(trackH);
	if (travel === 0) return 0.5;
	const raw = 1 - (offsetPx - THUMB_H / 2) / travel;
	return Math.min(1, Math.max(0, raw));
}
