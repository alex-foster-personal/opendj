/**
 * Wheel distance for the library track list (PVPIN-19, pin 535c07d9fe88).
 *
 * The track list scrolled by the browser's own wheel distance, which moved too
 * many rows per notch or swipe for a list of short rows. This scales the
 * VERTICAL distance of a pixel-mode wheel event by one factor and applies it
 * to the scroll container itself.
 *
 * Requirements:
 *   ✔︎ One wheel notch moves the list no more than a few rows.
 *     [if] a 120 px notch arrives [then] the list moves 120 * factor px ⛔️
 *   ✔︎ Direction and horizontal scrolling are unchanged.
 *     [if] deltaX is set [then] it is applied unscaled ⛔️
 *   ✔︎ Events the helper cannot scale honestly are left to the browser.
 *     [if] ctrlKey is held (pinch-zoom) or deltaMode is line/page [then] the
 *       default is not prevented ⛔️
 *   ✔︎ A factor of 1 is "off": nothing is intercepted, native scrolling runs.
 *     [if] the factor is outside (0, 1] [then] it throws ⛔️
 */

//-----------------------------------------------------------------------------
// config
//-----------------------------------------------------------------------------

/** Fraction of the browser's wheel distance the track list moves. A feel
 * number, not a derived one: 1 restores the browser default. */
export const LIBRARY_WHEEL_DISTANCE_FACTOR = 0.5;

const _DELTA_MODE_PIXEL = 0;

//-----------------------------------------------------------------------------
// pure
//-----------------------------------------------------------------------------

export interface LibraryWheelEventLike {
	deltaX: number;
	deltaY: number;
	deltaMode: number;
	ctrlKey: boolean;
}

export interface LibraryWheelScrollDelta {
	top: number;
	left: number;
}

/** The scroll to apply for this wheel event, or null to leave it to the browser. */
export function libraryWheelScrollDelta(
	event: LibraryWheelEventLike,
	factor: number = LIBRARY_WHEEL_DISTANCE_FACTOR
): LibraryWheelScrollDelta | null {
	if (!Number.isFinite(factor) || factor <= 0 || factor > 1) {
		throw new Error(`libraryWheelScrollDelta: factor must be in (0, 1], got ${factor}`);
	}
	if (factor === 1) return null;
	if (event.ctrlKey) return null;
	if (event.deltaMode !== _DELTA_MODE_PIXEL) return null;
	if (event.deltaX === 0 && event.deltaY === 0) return null;
	return { top: event.deltaY * factor, left: event.deltaX };
}

//-----------------------------------------------------------------------------
// action
//-----------------------------------------------------------------------------

/** Svelte action for the track list's scroll container. */
export function libraryWheelScroll(node: HTMLElement): { destroy: () => void } {
	const onWheel = (event: WheelEvent): void => {
		const delta = libraryWheelScrollDelta(event);
		if (delta === null) return;
		event.preventDefault();
		node.scrollTop += delta.top;
		node.scrollLeft += delta.left;
	};
	node.addEventListener('wheel', onWheel, { passive: false });
	return {
		destroy: () => node.removeEventListener('wheel', onWheel)
	};
}
