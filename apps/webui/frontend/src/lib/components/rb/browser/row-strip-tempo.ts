/**
 * The library row Preview strip's tempo curve (issue #7879): the deck strip's
 * curve (issue #8339, `wave/tempo-marks.ts`) in the 14 px row strip. The daemon
 * decides every segment; this only places the band.
 *
 * Band: a third of the strip under the vocal bar, no icons. The stroke is at
 * least 1 CSS px: the deck's 1.6 px at 40 px scaled to 14 px is 0.56 px, which
 * antialiases into a few faint pixels and reads as no curve at all (found on a
 * 55 percent tempo change in the live library, Thu 5 Nov 2026).
 */

import { drawTempoCurve, type TempoCurveData, type TempoLaneColors } from '../wave/tempo-marks';

export const ROW_STRIP_TEMPO_LINE_PX = 1;

/** The curve band inside a `stripH` px strip whose vocal bar is `vocalBarH` px tall. */
export function rowStripTempoBand(stripH: number, vocalBarH: number) {
	return { top: vocalBarH + 0.5, height: stripH / 3, lineWidth: ROW_STRIP_TEMPO_LINE_PX, iconPx: 0 };
}

export function drawRowStripTempo(
	ctx: CanvasRenderingContext2D,
	curve: TempoCurveData,
	durMs: number,
	w: number,
	stripH: number,
	vocalBarH: number,
	colors: TempoLaneColors
): void {
	drawTempoCurve(ctx, curve, 0, w / (durMs / 1000), w, rowStripTempoBand(stripH, vocalBarH), colors, 'fixed_10');
}
