/**
 * LIBUX-9754: the split-column library, two track tables side by side.
 *
 * rekordbox's browser split view (mimicked): each table is bound to its own pane,
 * so it keeps its own source, sort and selection. The active pane stays the one
 * the header (tabs, search, filters) acts on; clicking the other table makes it
 * active. Columns are pinned to panes, not to "active", so clicking does not swap
 * the two tables round. Pure: no Svelte, no DOM.
 */

/** The two panes the split view shows, left then right. */
export interface SplitPanes<Pane> {
	left: Pane;
	right: Pane;
}

/**
 * Which panes the split view shows. `right` is the pane pinned to the right
 * column; `lastLeft` is the pane the left column showed last. The active pane is
 * always on screen: left unless it is the pinned right pane. Null when there is
 * no second pane to show yet.
 */
export function splitPanes<Pane>(
	panes: readonly Pane[],
	active: Pane,
	right: Pane | null,
	lastLeft: Pane | null
): SplitPanes<Pane> | null {
	if (right === null || !panes.includes(right) || !panes.includes(active)) return null;
	if (active !== right) return { left: active, right };
	const left =
		lastLeft !== null && lastLeft !== right && panes.includes(lastLeft) ? lastLeft : null;
	const fallback = left ?? panes.find((pane) => pane !== right) ?? null;
	return fallback === null ? null : { left: fallback, right };
}

/** The pane the right column pins when split opens: the next tab after the active one, else the previous one, else null. */
export function pickSplitRight<Pane>(panes: readonly Pane[], active: Pane): Pane | null {
	const index = panes.indexOf(active);
	if (index < 0) throw new Error('pickSplitRight: the active pane is not in the pane list');
	return panes[index + 1] ?? panes[index - 1] ?? null;
}

/** The source a second table opens on when there is no neighbour tab: the library root (All Tracks). */
export const SPLIT_ROOT_PLAYLIST_ID = 'all';

/** How the right column gets its pane: pin a neighbour tab, or open a new tab on a real source. */
export type SplitRightPlan<Pane> =
	{ kind: 'pin'; pane: Pane } | { kind: 'open'; playlist_id: string };

/**
 * What split does for its right column. A neighbour tab is pinned as it is; with no
 * neighbour a new tab opens on the library root, so the second table never shows an
 * empty pane. Null when there is no neighbour and no free tab slot.
 */
export function planSplitRight<Pane>(
	panes: readonly Pane[],
	active: Pane,
	canAddPane: boolean
): SplitRightPlan<Pane> | null {
	const neighbour = pickSplitRight(panes, active);
	if (neighbour !== null) return { kind: 'pin', pane: neighbour };
	return canAddPane ? { kind: 'open', playlist_id: SPLIT_ROOT_PLAYLIST_ID } : null;
}

/** What a drop onto a split table needs to know about the pane under it. */
export interface SplitDropTarget {
	kind: string;
	playlist_id: string | null;
	title: string;
	writable: boolean;
}

/** Null when rows dropped on this table may be appended to its playlist, else the reason shown to the DJ. */
export function splitDropRefusal(target: SplitDropTarget, draggedCount: number): string | null {
	if (draggedCount === 0) return 'nothing was dragged';
	if (target.playlist_id === null)
		return 'this table has no list open - open a playlist in it first';
	if (target.kind !== 'playlist')
		return `${target.title} is not a playlist - drop onto a playlist table to add tracks`;
	if (!target.writable)
		return `${target.title} cannot be changed right now (still loading or read only)`;
	return null;
}
