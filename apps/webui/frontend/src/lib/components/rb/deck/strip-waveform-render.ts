/**
 * Pure canvas painters for the deck overview strip (COMPONENT-MAP 1.3).
 *
 * Extracted from StripWaveform.svelte so the strip is unit-testable the same
 * way the wavestack rows are (wave/render.ts). That seam is the point: the
 * strip silently omitted the engaged-loop band while the wavestack row drew
 * it, and nothing could catch it because the drawing lived inside a .svelte
 * file no unit test can import (DECKUX-04).
 *
 * Rendering honesty: preview bands are the REAL 400-point PWAV arrays, never
 * synthesized; vocal bars paint only for a real PVDI/demucs region; the loop
 * band paints only for an engaged loop the engine actually reports.
 */
import { type Vocals } from '$lib/rb/api-rb';
import type { AnlzWaveform, AnlzWaveformBands, LoopState } from '$lib/rb/types';
import { drawLoopRegion, VOCAL_BLUE, vocalAlpha } from '../wave/render';

/** Band colors per SCREENSHOT-SPEC 6: lows orange, mids blue, highs white. */
const BAND_LOW = '#e8a13a';
const BAND_MID = 'rgba(61, 125, 217, 0.85)';
const BAND_HIGH = 'rgba(207, 224, 242, 0.9)';
/** 'mono' payloads carry heights only - one color, never invented bands. */
const BAND_MONO = '#3d7dd9';

/** Vocal bar height on the strip's backing canvas. 4 backing px over the 40px
 * backing height reads as the ~2 CSS px the strip is scaled down to. */
const VOCAL_BAR_BACKING_PX = 4;

/** One frame of the deck overview strip, in backing-canvas pixels. */
export interface StripFrame {
	/** Backing canvas width (CSS scales it to the deck's strip width). */
	widthPx: number;
	/** Backing canvas height. */
	heightPx: number;
	/** Track length; null on an empty deck. Every x mapping needs it. */
	durationMs: number | null;
	/** Analysis waveform; null when the track has no analysis. */
	waveform: AnlzWaveform | null;
	/** Validated vocals of the loaded analysis; null when absent. */
	vocals: Vocals | null;
	/** Engaged loop reported by the engine; null when no loop is running. */
	loop: LoopState | null;
}

/**
 * Paint one full strip frame. The canvas is cleared first, so this is the
 * single entry point - callers never draw layers themselves.
 */
export function drawStripWaveform(ctx: CanvasRenderingContext2D, frame: StripFrame): void {
	const { widthPx: w, heightPx: h, durationMs } = frame;
	ctx.clearRect(0, 0, w, h);
	if (frame.waveform !== null) {
		_drawPreview(ctx, frame.waveform.preview, frame.waveform.kind, w, h);
		if (frame.vocals !== null && durationMs !== null && durationMs > 0) {
			_drawVocalBars(ctx, frame.vocals, durationMs, w);
		}
	}
	// The loop band is NOT gated on analysis: it overlays the strip rect
	// itself, so an engaged loop stays visible on a track whose ANLZ is
	// missing. An engaged loop is never invisible on a surface that has a
	// duration to place it with (DECKUX-04).
	if (durationMs !== null && durationMs > 0) {
		drawLoopRegion(ctx, frame.loop, (ms) => (ms / durationMs) * w, w, h);
	}
}

// ----------------------------------------------------------- _helpers

function _bar(
	ctx: CanvasRenderingContext2D,
	x: number,
	w: number,
	v: number,
	color: string,
	heightPx: number
): void {
	const h = Math.max(0, Math.min(1, v)) * heightPx;
	ctx.fillStyle = color;
	ctx.fillRect(x, heightPx - h, w, h);
}

function _drawPreview(
	ctx: CanvasRenderingContext2D,
	bands: AnlzWaveformBands,
	kind: 'tri' | 'mono',
	widthPx: number,
	heightPx: number
): void {
	const n = bands.length;
	if (n === 0) return;
	const w = widthPx / n;
	for (let i = 0; i < n; i++) {
		const x = i * w;
		if (kind === 'tri') {
			_bar(ctx, x, w, bands.low[i], BAND_LOW, heightPx);
			_bar(ctx, x, w, bands.mid[i], BAND_MID, heightPx);
			_bar(ctx, x, w, bands.high[i], BAND_HIGH, heightPx);
		} else {
			// mono = heights only; single color, never synthesized bands.
			const v = Math.max(bands.low[i], bands.mid[i], bands.high[i]);
			_bar(ctx, x, w, v, BAND_MONO, heightPx);
		}
	}
}

function _drawVocalBars(
	ctx: CanvasRenderingContext2D,
	vocals: Vocals,
	durationMs: number,
	widthPx: number
): void {
	// rekordbox + demucs render identically; barless states draw nothing.
	if (vocals.status !== 'rekordbox' && vocals.status !== 'demucs') return;
	ctx.fillStyle = VOCAL_BLUE;
	for (const region of vocals.regions) {
		const x0 = Math.max(0, ((region.start_s * 1000) / durationMs) * widthPx);
		const x1 = Math.min(widthPx, ((region.end_s * 1000) / durationMs) * widthPx);
		if (x1 <= x0) continue;
		ctx.globalAlpha = vocalAlpha(region.intensity);
		ctx.fillRect(x0, 0, x1 - x0, VOCAL_BAR_BACKING_PX);
	}
	ctx.globalAlpha = 1;
}
