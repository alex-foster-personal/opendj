/**
 * Counts track rows actually intersecting the viewport, for the ui-mirror
 * `browser.visible_rows_count` field (#3096).
 *
 * TrackTable.svelte virtualizes the row list (`computeVirtualWindow`,
 * `virtual-window.ts`): the mounted DOM band is the visible rows PLUS an
 * `OVERSCAN` pad of extra rows above and below, kept mounted off-screen for
 * smooth scrolling. So `document.querySelectorAll(TRACK_ROW_SELECTOR).length`
 * alone counts overscan rows too and overstates what is actually on screen -
 * this module exists to filter that DOM count down to genuine intersection.
 *
 * ui-mirror.ts runs outside any Svelte component, so it has no reactive
 * access to TrackTable's own scrollTop/viewportHeight state; the only signal
 * available to an external reader is DOM geometry, hence getBoundingClientRect
 * rather than the scrollTop-index math `virtual-window.ts` uses internally.
 *
 * Geometry alone is not enough either: LIBUX-05's "technically-working mode"
 * (`/performance`, the only route this mirror runs on) hides the whole
 * BrowserPanel by setting opacity + pointer-events on ITS OWN root element,
 * never a wrapper div (each region owns its `grid-area`). A hidden panel's
 * rows keep their normal on-page rects - opacity is a paint-time effect, not
 * a layout one - so a geometry-only check reads a positive count while the
 * screen shows nothing. `_isPaintedChain` walks the container's ancestors
 * for the opacity/visibility/display an intersection rect cannot see.
 */

const TRACK_ROW_SELECTOR = '[data-testid="track-row"]';
/** TrackTable.svelte's scrollable wrapper (`bind:this={wrapEl}`, `class="table-wrap"`).
 * A row's own rect can be geometrically positioned inside the page even while
 * clipped out of sight by this container's `overflow`, which is exactly what
 * an overscan row is - so intersection must be checked against THIS rect, not
 * just the window's. */
const SCROLL_CONTAINER_SELECTOR = '.table-wrap';

interface Rect {
	top: number;
	left: number;
	right: number;
	bottom: number;
	width: number;
	height: number;
}

function _rectsIntersect(row: Rect, bounds: Rect): boolean {
	return (
		row.width > 0 &&
		row.height > 0 &&
		row.left < bounds.right &&
		row.right > bounds.left &&
		row.top < bounds.bottom &&
		row.bottom > bounds.top
	);
}

/** True while `element` and every ancestor up to the document root paint:
 * no `opacity: 0`, `visibility: hidden`, or `display: none` anywhere in the
 * chain. Checked once against the scroll container, not per row, since
 * LIBUX-05 hides the shared BrowserPanel ancestor the container and every
 * row sit inside, not any row individually. */
function _isPaintedChain(element: Element): boolean {
	let node: Element | null = element;
	while (node !== null) {
		const style = getComputedStyle(node);
		if (style.opacity === '0' || style.visibility === 'hidden' || style.display === 'none') {
			return false;
		}
		node = node.parentElement;
	}
	return true;
}

/** Rows currently on screen: present in the DOM, inside the scroll
 * container's clipped viewport, inside the browser window, AND on an
 * unhidden paint chain. Reads 0 when no track row is mounted (empty
 * library, or the browser panel's `{#if}`-collapsed on the library page),
 * when every mounted row is scrolled out of the container's or the window's
 * visible bounds, or when the panel is opacity-hidden by LIBUX-05's
 * technically-working mode. */
export function countVisibleTrackRows(): number {
	const rows = [...document.querySelectorAll(TRACK_ROW_SELECTOR)];
	if (rows.length === 0) return 0;
	const windowRect: Rect = {
		top: 0,
		left: 0,
		right: window.innerWidth,
		bottom: window.innerHeight,
		width: window.innerWidth,
		height: window.innerHeight
	};
	return rows.filter((row) => {
		const container = row.closest(SCROLL_CONTAINER_SELECTOR);
		if (container === null) {
			throw new Error(
				`countVisibleTrackRows: found a track row with no ancestor matching "${SCROLL_CONTAINER_SELECTOR}" - TrackTable.svelte's wrapper markup moved`
			);
		}
		if (!_isPaintedChain(container)) return false;
		const containerRect = container.getBoundingClientRect();
		const rect = row.getBoundingClientRect();
		return _rectsIntersect(rect, containerRect) && _rectsIntersect(rect, windowRect);
	}).length;
}
