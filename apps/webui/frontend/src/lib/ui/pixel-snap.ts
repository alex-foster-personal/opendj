/**
 * Snap a playhead fraction to the device pixel grid it is drawn on.
 *
 * A playing deck moves its clock every frame, but a marker drawn across a
 * strip a few hundred pixels wide crosses a device pixel only every few
 * hundred milliseconds. Binding the raw fraction to a style or attribute
 * rewrote it every frame anyway, and each rewrite cost a style recalc, a
 * layout or a repaint of whatever surrounds the marker: with two decks
 * playing that was a large share of every frame (Fri 9 Oct 2026). A snapped
 * value is equal from frame to frame until the marker would actually move,
 * so Svelte skips the DOM write in between.
 *
 * `fraction` must be finite and is clamped to [0, 1]; NaN or an infinite value
 * is a corrupt playhead, and throws rather than freezing the marker at an end.
 * `devicePx` is the length of the travel in
 * DEVICE pixels (CSS length times devicePixelRatio). `null` or 0 means the
 * size is not measured yet (a `bind:clientWidth` before layout), and the
 * fraction is returned unsnapped so the marker still draws where it belongs.
 * Any other value that is not a finite positive number (NaN, infinite,
 * negative) is a broken layout or density input, and throws.
 */
export function snapFractionToPixels(fraction: number, devicePx: number | null): number {
	if (!Number.isFinite(fraction)) {
		throw new RangeError(`snapFractionToPixels: fraction must be a finite number, got ${fraction}`);
	}
	const clamped = Math.min(1, Math.max(0, fraction));
	if (devicePx === null || devicePx === 0) return clamped;
	if (!Number.isFinite(devicePx) || devicePx < 0) {
		throw new RangeError(`snapFractionToPixels: devicePx must be null, 0 or a finite positive number, got ${devicePx}`);
	}
	return Math.round(clamped * devicePx) / devicePx;
}
