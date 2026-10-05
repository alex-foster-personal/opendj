/**
 * Pure painter for the Settings waveform thumbnail (WaveformDesignPreview).
 *
 * Kept out of the component so a unit test can paint every design x palette
 * pair into a recording context and prove each one draws visible marks. An
 * unknown design throws rather than falling through to some other look, so a
 * new design added without a preview branch fails loud (Mon 5 Oct 2026).
 */
import type { WaveformDesign } from '$lib/rb/waveform-design';
import type { WaveBandColors } from '$lib/rb/wave-palette';

/** The dark fill every preview is painted on (the colors are its dark-face hues). */
export const PREVIEW_BACKGROUND = '#0a0c10';
const BAR_COUNT = 48;

/** The minimal 2D-context surface the painter uses, so a test can record it. */
export interface PreviewPaintContext {
	fillStyle: string | CanvasGradient | CanvasPattern;
	strokeStyle: string | CanvasGradient | CanvasPattern;
	lineWidth: number;
	clearRect(x: number, y: number, w: number, h: number): void;
	fillRect(x: number, y: number, w: number, h: number): void;
	beginPath(): void;
	moveTo(x: number, y: number): void;
	lineTo(x: number, y: number): void;
	stroke(): void;
}

function _sampleEnvelope(): number[] {
	return Array.from({ length: BAR_COUNT }, (_, i) => {
		const t = i / (BAR_COUNT - 1);
		return 0.15 + 0.75 * Math.abs(Math.sin(t * Math.PI * 3)) * (1 - t * 0.2);
	});
}

export function paintWaveformPreview(
	ctx: PreviewPaintContext,
	w: number,
	h: number,
	design: WaveformDesign,
	c: WaveBandColors
): void {
	ctx.clearRect(0, 0, w, h);
	ctx.fillStyle = PREVIEW_BACKGROUND;
	ctx.fillRect(0, 0, w, h);
	const sample = _sampleEnvelope();
	const barW = w / BAR_COUNT;
	switch (design) {
		case 'line': {
			ctx.strokeStyle = c.mono;
			ctx.lineWidth = 1.5;
			ctx.beginPath();
			sample.forEach((v, i) => {
				const x = i * barW + barW / 2;
				const y = h - v * (h - 4);
				if (i === 0) ctx.moveTo(x, y);
				else ctx.lineTo(x, y);
			});
			ctx.stroke();
			return;
		}
		case 'blocks':
			// Mirrored single-color bars with a 1px gap (waveform-blocks-design).
			ctx.fillStyle = c.mono;
			sample.forEach((v, i) => {
				const bh = v * (h - 2);
				ctx.fillRect(i * barW, (h - bh) / 2, Math.max(1, barW - 1), bh);
			});
			return;
		case 'mono':
			ctx.fillStyle = c.mono;
			sample.forEach((v, i) => {
				const bh = v * (h - 2);
				ctx.fillRect(i * barW, h - bh, barW, bh);
			});
			return;
		case 'tri-band':
			sample.forEach((v, i) => {
				const x = i * barW;
				const amp = v * (h - 2);
				ctx.fillStyle = c.low;
				ctx.fillRect(x, h - amp * 0.55, barW, amp * 0.55);
				ctx.fillStyle = c.mid;
				ctx.fillRect(x, h - amp * 0.75, barW, amp * 0.35);
				ctx.fillStyle = c.high;
				ctx.fillRect(x, h - amp * 0.45, barW, amp * 0.25);
			});
			return;
		default: {
			const _exhaustive: never = design;
			throw new Error(`waveform preview has no painter for design ${String(_exhaustive)}`);
		}
	}
}
