/**
 * Pane tab reordering + new-tab placement.
 *
 * Split out of pane-contract.svelte.ts: distinct concern from the PaneStore
 * reactive contract - plain array/index math over whatever pane list the
 * caller holds, no $state involved. Kept plain .ts so it loads under the
 * same node:test harness as its sibling.
 *
 * RECOVERED, not designed. `BrowserPanel.svelte` has imported both of these
 * since cfbfe55 ("wip(spike)", Fri 24 Jul 2026 01:19) but they were never
 * committed anywhere -- not in the branch, not in the Cursor backup snapshot
 * 718cc81 -- so /performance could not build for anyone. Reconstructed from
 * the two call sites; semantics are inferred, so treat as provisional and
 * replace if the original turns up.
 */

/**
 * Move a pane tab and return where the ACTIVE pane ended up.
 *
 * Mutates ``panes`` in place because the caller holds the same array
 * reference (hence "InPlace" in the name); returns the new active index
 * rather than mutating it, since the caller owns that state.
 */
export function reorderPanesInPlace<T>(
	panes: T[],
	from: number,
	to: number,
	activePane: number
): number {
	if (
		from === to ||
		from < 0 ||
		to < 0 ||
		from >= panes.length ||
		to >= panes.length
	) {
		return activePane;
	}
	const [moved] = panes.splice(from, 1);
	panes.splice(to, 0, moved);
	// Follow the dragged tab if it was the active one; otherwise shift only
	// when the move crossed the active index.
	if (activePane === from) return to;
	if (from < activePane && to >= activePane) return activePane - 1;
	if (from > activePane && to <= activePane) return activePane + 1;
	return activePane;
}

/**
 * Index of the tab a newly opened playlist should take, or null if none is free.
 *
 * Null means every tab is sticky (locked); the caller surfaces that as an
 * explicit toast rather than silently stealing a locked tab.
 */
export function resolveNewTabIndex(panes: { sticky?: boolean }[]): number | null {
	const free = panes.findIndex((p) => p.sticky !== true);
	return free === -1 ? null : free;
}

/** Maximum browser pane tabs (SCREENSHOT-SPEC 5c). */
export const MAX_PANE_SLOTS = 4;

/** True while another blank pane slot can be opened via Blank List (+). */
export function canAddPaneSlot(paneCount: number): boolean {
	return paneCount < MAX_PANE_SLOTS;
}
