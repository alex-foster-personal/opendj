/**
 * Lyric rows under the hot-cue pads (LYR-14). The deck's cue column stacks
 * the 2x4 pad bank above the lyric strip, so the strip may only take what
 * the pads can spare: the pads keep at least PAD_BANK_MIN_PX, and the strip
 * shows the current line plus one preview line when that leaves room, else
 * the current line alone.
 */
export const PAD_BANK_MIN_PX = 60;
/** Strip height for the current line (18px) plus one preview row (12px). */
export const LYRIC_TWO_ROW_PX = 34;
/** Strip height for the current line alone, compact. */
export const LYRIC_ONE_ROW_PX = 16;

/** Map the measured cue-column height to how many lyric lines fit under the pads. */
export function deckLyricRowBudget(cueFlexHeightPx: number): 1 | 2 {
	if (!Number.isFinite(cueFlexHeightPx) || cueFlexHeightPx <= 0) return 1;
	return cueFlexHeightPx >= PAD_BANK_MIN_PX + LYRIC_TWO_ROW_PX ? 2 : 1;
}

/** Height the lyric strip takes under the pads for a row budget. */
export function deckLyricStripPx(rows: 1 | 2): number {
	return rows === 2 ? LYRIC_TWO_ROW_PX : LYRIC_ONE_ROW_PX;
}
