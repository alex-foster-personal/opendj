/**
 * The performance page's mixer column width (`.deck-area`'s middle track).
 * The mixer's S button widens it 20% so each channel can stand its stem dials
 * beside the EQ (the maintainer, Mon 2 Nov 2026: "you can widen the mixer 20% for them
 * when on").
 */

/** The column the mixer has always had: four strips across. */
export const MIXER_COLUMN_PX = 248;
/** Width factor while the S button is on. */
export const MIXER_STEMS_WIDEN = 1.2;

export function mixerColumnPx(mixerStems: boolean): number {
	return mixerStems ? MIXER_COLUMN_PX * MIXER_STEMS_WIDEN : MIXER_COLUMN_PX;
}
