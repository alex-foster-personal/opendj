/**
 * Knob caption fit (COLORFX-10): a caption never grows wider than its knob
 * column, so two neighbouring captions ("FILTER ▾" beside "DRUMS") cannot touch.
 *
 * The column is the dial plus a fixed overhang on each side. A caption that
 * does not fit at the full 8px size drops to 7px; one that still does not fit
 * is cut with an ellipsis by CSS (Knob.svelte `.label-text`), and the full
 * label stays on the knob's title and aria-label.
 */

export const KNOB_CAPTION_CFG = {
	/** Full caption size, px (Knob.svelte's historic 8px). */
	FULL_FONT_PX: 8,
	/** Smallest size before the ellipsis takes over, px. */
	MIN_FONT_PX: 7,
	/** Measured advance of one caption glyph at 1px font size, incl. the 0.04em tracking. */
	GLYPH_ADVANCE_PER_FONT_PX: 0.65,
	/** Caption may overhang the dial by this much in total (half each side), px. */
	OVERHANG_PX: 18
} as const;

export interface KnobCaptionFit {
	/** Caption box max width: the knob column. */
	maxWidthPx: number;
	/** Font size the caption renders at. */
	fontPx: number;
}

/** Estimated rendered width of `label` at `fontPx`, plus a fixed trailing mark. */
export function estimateCaptionWidthPx(label: string, fontPx: number, trailPx: number): number {
	return label.length * KNOB_CAPTION_CFG.GLYPH_ADVANCE_PER_FONT_PX * fontPx + trailPx;
}

/**
 * Fit a caption to the column of a `dialSizePx` dial. `trailPx` is the width
 * of any in-flow mark after the text (the color FX chevron plus its gap).
 */
export function fitKnobCaption(label: string, dialSizePx: number, trailPx = 0): KnobCaptionFit {
	if (!Number.isFinite(dialSizePx) || dialSizePx <= 0) {
		throw new Error(`fitKnobCaption: dial size must be a positive finite number, got ${dialSizePx}`);
	}
	if (!Number.isFinite(trailPx) || trailPx < 0) {
		throw new Error(`fitKnobCaption: trail width must be a finite number >= 0, got ${trailPx}`);
	}
	const maxWidthPx = dialSizePx + KNOB_CAPTION_CFG.OVERHANG_PX;
	const fits = (fontPx: number) => estimateCaptionWidthPx(label, fontPx, trailPx) <= maxWidthPx;
	const fontPx = fits(KNOB_CAPTION_CFG.FULL_FONT_PX) ? KNOB_CAPTION_CFG.FULL_FONT_PX : KNOB_CAPTION_CFG.MIN_FONT_PX;
	return { maxWidthPx, fontPx };
}
