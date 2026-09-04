/** Shared fixed-position placement for interactive column-header explainers. */
export type ColumnExplainerPlacement = 'above' | 'below';

const PANEL_GAP_PX = 6;
const VIEWPORT_MARGIN_PX = 8;

function _clamp(value: number, lower: number, upper: number): number {
	return Math.max(lower, Math.min(value, Math.max(lower, upper)));
}

export function columnExplainerStyle(
	rect: Pick<DOMRect, 'left' | 'top' | 'bottom'>,
	panel: Pick<DOMRect, 'width' | 'height'>,
	viewport: Pick<Window, 'innerWidth' | 'innerHeight'>,
	placement: ColumnExplainerPlacement
): string {
	const left = _clamp(
		rect.left - panel.width - PANEL_GAP_PX,
		VIEWPORT_MARGIN_PX,
		viewport.innerWidth - panel.width - VIEWPORT_MARGIN_PX
	);
	const above = rect.top - panel.height - PANEL_GAP_PX;
	const below = rect.bottom + PANEL_GAP_PX;
	const aboveWouldClip = above < VIEWPORT_MARGIN_PX;
	const belowFits = below + panel.height <= viewport.innerHeight - VIEWPORT_MARGIN_PX;
	const desiredTop = placement === 'above' && aboveWouldClip && belowFits ? below : placement === 'above' ? above : below;
	const top = _clamp(
		desiredTop,
		VIEWPORT_MARGIN_PX,
		viewport.innerHeight - panel.height - VIEWPORT_MARGIN_PX
	);
	return `left:${Math.round(left)}px;top:${Math.round(top)}px;`;
}
