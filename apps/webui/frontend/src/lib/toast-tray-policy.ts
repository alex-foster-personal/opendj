/**
 * Toast tray depth and layout policy (issue #3996, UX-TOAST-03).
 * Pure helpers: no stores or DOM.
 */

export const TOAST_MAX_VISIBLE = 3;
export const TOAST_VIEWPORT_WIDTH_FRACTION = 1 / 3;
export const TOAST_EXIT_DURATION_MS = 100;
export const TOAST_EXIT_TRANSLATE_PX = 100;

/** Newest at end; visible slice is the last N non-exiting toasts, plus any exiting (animating out). */
export function selectVisibleToasts<T extends { exiting?: boolean }>(
	all: readonly T[],
	maxVisible: number = TOAST_MAX_VISIBLE
): T[] {
	const nonExiting = all.filter((t) => t.exiting !== true);
	const visibleNonExiting = new Set(nonExiting.slice(-maxVisible));
	return all.filter((t) => t.exiting === true || visibleNonExiting.has(t));
}

/** Index of the oldest non-exiting toast to evict when over capacity, or -1. */
export function oldestNonExitingIndex<T extends { exiting?: boolean }>(
	all: readonly T[]
): number {
	return all.findIndex((t) => t.exiting !== true);
}
