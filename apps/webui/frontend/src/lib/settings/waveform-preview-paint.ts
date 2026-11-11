/**
 * Pure painter for the Settings waveform thumbnail (WaveformDesignPreview).
 *
 * Kept out of the component so a unit test can paint every design x palette
 * pair into a recording context and prove each one draws visible marks. An
 * unknown design throws rather than falling through to some other look, so a
 * new design added without a preview branch fails loud (Mon 5 Oct 2026).
 *
 * The bands are painted by the DECK renderer's own shading code
 * (render.ts drawWaveBands) over a real 3-band capture, never by a
 * painter of its own (W15, CHROME-18, Tue 6 Oct 2026): the old copy filled
 * every 'blocks' bar with the one mono color, so Gothic's grayscale-shaded
 * blocks showed as flat white bars in Settings.
 */
import { drawBeatGridLines, drawWaveBands, type BandPalette } from '$lib/components/rb/wave/render';
import { visibleBeatLines } from '$lib/components/rb/wave/wave-math';
import type { WaveGridLines } from '$lib/rb/wave-grid-lines';
import { PREVIEW_SAMPLE_BEATS, PREVIEW_SAMPLE_SECONDS } from './waveform-preview-beats';
import type { AnlzWaveform } from '$lib/rb/anlz-types';
import type { WaveHeight } from '$lib/rb/wave-height';
import type { WaveformDesign } from '$lib/rb/waveform-design';

/** The dark fill every preview is painted on (the colors are its dark-face hues). */
export const PREVIEW_BACKGROUND = '#0a0c10';
/** The preview's beat grid color (line alphas come from the geometry). */
export const PREVIEW_GRID_COLOR = '#ffffff';
const _DESIGNS: ReadonlySet<string> = new Set<WaveformDesign>(['tri-band', 'mono', 'line', 'blocks', 'rgb']);

export function paintWaveformPreview(
	ctx: CanvasRenderingContext2D,
	w: number,
	h: number,
	design: WaveformDesign,
	c: BandPalette,
	sample: AnlzWaveform | null,
	waveHeight: WaveHeight = 'full',
	gridLines: WaveGridLines = 'off'
): void {
	if (!_DESIGNS.has(design)) {
		throw new Error(`waveform preview has no painter for design ${String(design)}`);
	}
	ctx.clearRect(0, 0, w, h);
	ctx.fillStyle = PREVIEW_BACKGROUND;
	ctx.fillRect(0, 0, w, h);
	// null while the sample chunk loads (waveform-preview-sample.ts is a lazy
	// chunk so the library route's first load stays in its gzip budget):
	// background only, never an invented waveform.
	if (sample === null) return;
	drawWaveBands(ctx, sample, w, h, c, design, 0, waveHeight);
	// issue #10244: the deck's own grid geometry and painter over real beats.
	const lines = visibleBeatLines(
		PREVIEW_SAMPLE_BEATS,
		0,
		w / PREVIEW_SAMPLE_SECONDS,
		w,
		h,
		false,
		undefined,
		undefined,
		gridLines
	);
	drawBeatGridLines(ctx, lines, PREVIEW_GRID_COLOR);
}

export type WaveformPreviewPaintFn = typeof paintWaveformPreview;
