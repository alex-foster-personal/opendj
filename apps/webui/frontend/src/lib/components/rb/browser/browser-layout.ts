/**
 * LIBUX-9743: the browser grid's column template, rail | tree | divider | list.
 *
 * Single-column (rekordbox's browser layout toggle, mimicked) collapses the tree
 * and its divider to zero so the track list takes the full width. The stored
 * tree width is an input, never rewritten here, so leaving single-column puts
 * the tree back at exactly the width it had.
 */

/** Width of the recorder rail at the browser's left edge, in CSS pixels. */
export const BROWSER_RAIL_WIDTH_PX = 30;
/** Width of the tree resize divider, in CSS pixels. */
export const BROWSER_TREE_DIVIDER_PX = 6;

/** The grid-template-columns value for the browser panel. */
export function browserGridColumns(singleColumn: boolean, treeWidthPx: number): string {
	if (!Number.isFinite(treeWidthPx) || treeWidthPx <= 0) {
		throw new RangeError(
			`browserGridColumns: tree width must be a positive number; got ${treeWidthPx}`
		);
	}
	if (singleColumn) return `${BROWSER_RAIL_WIDTH_PX}px 0px 0px minmax(0, 1fr)`;
	return `${BROWSER_RAIL_WIDTH_PX}px ${treeWidthPx}px ${BROWSER_TREE_DIVIDER_PX}px minmax(0, 1fr)`;
}
