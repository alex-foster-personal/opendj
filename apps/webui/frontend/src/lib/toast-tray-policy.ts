/**
 * Toast tray depth and layout policy (issue #3996, UX-TOAST-03).
 * Pure helpers: no stores or DOM.
 */

export const TOAST_MAX_VISIBLE = 3;
export const TOAST_VIEWPORT_WIDTH_FRACTION = 1 / 3;
export const TOAST_EXIT_DURATION_MS = 100;
export const TOAST_EXIT_TRANSLATE_PX = 100;

/** Hard render budget: at most maxVisible nodes; exiting rows count toward the cap, not on top. */
export function selectVisibleToasts<T extends { exiting?: boolean }>(
	all: readonly T[],
	maxVisible: number = TOAST_MAX_VISIBLE
): T[] {
	if (all.length === 0 || maxVisible <= 0) return [];
	const exiting = all.filter((t) => t.exiting === true);
	const nonExiting = all.filter((t) => t.exiting !== true);
	const exitingSlots = Math.min(exiting.length, maxVisible);
	const activeSlots = maxVisible - exitingSlots;
	const visibleExiting = exiting.slice(0, exitingSlots);
	const visibleNonExiting = nonExiting.slice(-Math.min(nonExiting.length, activeSlots));
	const visibleSet = new Set([...visibleExiting, ...visibleNonExiting]);
	return all.filter((t) => visibleSet.has(t));
}

/** Index of the oldest non-exiting toast to evict when over capacity, or -1. */
export function oldestNonExitingIndex<T extends { exiting?: boolean }>(
	all: readonly T[]
): number {
	return all.findIndex((t) => t.exiting !== true);
}
