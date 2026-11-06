/**
 * Justified debounce for every custom tooltip (issue #927, pin 946e04da2d0d).
 * Each band states delay and why so instant vs skim vs flyby-swallow is explicit.
 */
export type TooltipTiming = { delay: number; why: string };

export function tooltipTiming(t: TooltipTiming): TooltipTiming {
	if (!Number.isFinite(t.delay) || t.delay < 0) {
		throw new Error(`tooltip delay must be a finite number >= 0, got ${t.delay}`);
	}
	const why = t.why.trim();
	if (why.length < 10) {
		throw new Error(`tooltip why must be at least 10 characters, got ${why.length}`);
	}
	return { delay: t.delay, why };
}

/** Packed topbar icon row: drag along the list and read like a menu (pin 79d3616ab3d9). */
export const TOOLTIP_SKIM_ROW = tooltipTiming({
	delay: 10,
	why: 'Packed topbar icon row: drag along the list and read like a menu (pin 79d3616ab3d9). Instant would flash every icon; slower than 10 ms feels like lag on a skim.'
});

/** Adjacent controls: swallow a pointer flyby without lag on a deliberate hover (pin 946e04da2d0d). */
export const TOOLTIP_PACKED = tooltipTiming({
	delay: 50,
	why: 'Adjacent controls (NEXT chevron, document-layer titles, column headers). Swallows a pointer flyby without lag on a deliberate hover (pin 946e04da2d0d).'
});

/** Headphone device pickers in a vertical stack; pointer runs down the column. */
export const TOOLTIP_HEADPHONE_STACK = tooltipTiming({
	delay: 40,
	why: 'Headphone device pickers sit in a vertical stack (placement right); 40 ms lets the pointer run down the column without opening every row.'
});

/** MIX / VOL / mode / split in a tight cluster on the top bar. */
export const TOOLTIP_HEADPHONE_CLUSTER = tooltipTiming({
	delay: 60,
	why: 'MIX / VOL / mode / split sit in a tight cluster; 60 ms swallows a crossing of the mixer without the 10 ms menu-skim of the topbar.'
});

/** Infrequent, copy-heavy warnings (rescan, head delay, dual-sink). */
export const TOOLTIP_INFREQUENT = tooltipTiming({
	delay: 100,
	why: 'Rescan / HEAD DELAY / dual-sink warnings. Infrequent and copy-heavy; a longer rest stops a pass across the heading from opening them.'
});

/** Deck header chrome over the waveform; avoid covering jog during a crossing. */
export const TOOLTIP_DECK_CHROME = tooltipTiming({
	delay: 150,
	why: 'Deck header chrome sits over the waveform; 150 ms avoids covering jog/waveform during a pointer crossing.'
});

/** Isolated control; nothing adjacent would flash on a flyby. */
export const TOOLTIP_ISOLATED = tooltipTiming({
	delay: 0,
	why: 'Isolated control; nothing adjacent would flash on a flyby, so instant is safe.'
});

/** Truncated track names: sweeping across rows must show nothing (LIBUX-57). */
export const TOOLTIP_TRUNCATION = tooltipTiming({
	delay: 600,
	why: 'Truncated track names: sweeping across rows must show nothing until the pointer rests on one clipped label.'
});

/** @deprecated Use TOOLTIP_TRUNCATION.delay */
export const TRUNCATION_TIP_DELAY_MS = TOOLTIP_TRUNCATION.delay;
