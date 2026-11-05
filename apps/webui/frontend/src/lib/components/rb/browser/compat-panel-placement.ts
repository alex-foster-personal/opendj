/**
 * Where the fixed-position compatible dropdown sits against its checkbox
 * (PR #6574). Pure, so it is unit-tested without a DOM. Below when the whole
 * panel fits, else on the side with more room; either way the height is capped
 * to that side's space and the panel scrolls, so switching Mashup to Transition
 * (which adds the rule checkboxes) never grows it off-screen.
 */
export interface AnchorRect {
	left: number;
	top: number;
	bottom: number;
}

export interface PanelPlacement {
	left: number;
	top: number;
	maxHeight: number;
}

/** Gap kept between the panel and the window edge, and to the anchor. */
export const PANEL_EDGE_GAP = 8;
const ANCHOR_GAP = 4;

export function compatPanelPlacement(
	anchor: AnchorRect,
	naturalHeight: number,
	width: number,
	viewport: { width: number; height: number }
): PanelPlacement {
	const left = Math.max(
		PANEL_EDGE_GAP,
		Math.min(anchor.left, viewport.width - width - PANEL_EDGE_GAP)
	);
	const belowTop = anchor.bottom + ANCHOR_GAP;
	const roomBelow = Math.max(0, viewport.height - PANEL_EDGE_GAP - belowTop);
	const roomAbove = Math.max(0, anchor.top - ANCHOR_GAP - PANEL_EDGE_GAP);
	if (naturalHeight <= roomBelow || roomBelow >= roomAbove) {
		return { left, top: belowTop, maxHeight: roomBelow };
	}
	const height = Math.min(naturalHeight, roomAbove);
	return { left, top: anchor.top - ANCHOR_GAP - height, maxHeight: roomAbove };
}

export function placementStyle(p: PanelPlacement): string {
	return `left:${p.left}px;top:${p.top}px;max-height:${p.maxHeight}px`;
}
