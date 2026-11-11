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
 * band paints only for an engaged loop the engine actually reports; the tempo
 * curve paints only the daemon's own tempo-stability segments (issue #8339).
 */
import {
	BLOCK_BAR_PX,
	BLOCK_PITCH_PX,
	drawLoopRegion,
	resolveStripWaveformKind,
	vocalAlpha
} from '../wave/render';
import { rgbColumnColor } from '../wave/wave-band-layers';
import type { WaveformDesign } from '$lib/rb/waveform-design';
import type { BandGains } from '$lib/rb/wave-eq-gains.svelte';
import { resolveStripBandColors, type WaveBandColors } from '$lib/rb/wave-palette';
import { loopBandPx, type LoopBandSource } from '../wave/wave-math';
import type { SectionPalette, SectionSpan } from '../wave/sections';
import type { SectionsApi } from '../wave/sections-lazy.svelte';
import {
	drawTempoCurve,
	TEMPO_LANE_COLORS,
	type TempoCurveData,
	type TempoLaneColors,
	type TempoLaneScale
} from '../wave/tempo-marks';

export { TEMPO_LANE_COLORS };

// This module is PRESENTATIONAL: it names the shapes it paints instead of
// importing `$lib/rb/types` or `$lib/rb/api-rb`. Two reasons, in order:
// a painter that depends only on the numbers it draws can be unit-tested
// without the API contract behind it, and `types.ts` is the most-imported
// module in the frontend, so every avoidable edge into it is worth not adding
// (scripts/quality_gate.py tracks this as frontend.max_fan_in).
// These are structural subsets of AnlzWaveform / AnlzWaveformBands / Vocals,
// so callers keep passing the real objects with no conversion.

/** Band colors when the caller passes none: the default rekordbox 3Band
 * palette on the dark face (issue #4219). Callers pass the user's choice via
 * `resolveStripBandColors(theme, wave_palette)`. 'mono' payloads carry
 * heights only - one color (`mono`), never invented bands. */
const DEFAULT_BAND_COLORS: WaveBandColors = resolveStripBandColors('dark');
/** Stored loop hot-cue span, distinct from the translucent engaged-loop band. */
export const LOOP_CUE_COLOR = '#e8a13a';
const LOOP_CUE_OUTLINE = '#c8cdd2';
const LOOP_CUE_MARKER_PX = 8;

/** Vocal bar height on the strip's backing canvas. 4 backing px over the 40px
 * backing height reads as the ~2 CSS px the strip is scaled down to. */
const VOCAL_BAR_BACKING_PX = 4;

/** Tempo curve (issue #8339) on the strip: a band a third of the strip tall,
 * just below the vocal bar, sized for the 40 px reference backing height and
 * scaled with the real one (see wave/tempo-marks.ts for the design). */
const TEMPO_CURVE_REFERENCE_HEIGHT_PX = 40;
const TEMPO_CURVE_LINE_PX = 1.6;
const TEMPO_CURVE_ICON_PX = 13;

/** Same-length normalized 0..1 energy arrays, one per frequency band. */
export interface StripBands {
	length: number;
	low: readonly number[];
	mid: readonly number[];
	high: readonly number[];
}

/** Only the preview strip is drawn here; the detail array is the wavestack's. */
export interface StripWaveformBands {
	kind: 'tri' | 'mono';
	preview: StripBands;
}

/** One PVDI/demucs vocal region, in track seconds. */
export interface StripVocalRegion {
	start_s: number;
	end_s: number;
	intensity: number;
}

/** Vocal detection result. The two barless states carry no regions to paint. */
export type StripVocals =
	| { status: 'rekordbox'; regions: readonly StripVocalRegion[] }
	| { status: 'demucs'; regions: readonly StripVocalRegion[] }
	| { status: 'no_vocals'; regions: readonly StripVocalRegion[] }
	| { status: 'not_analyzed' };

/** Persisted loop hot-cue bounds from the deck's real hot-cue bank. */
export interface StripLoopCue {
	in_ms: number;
	out_ms: number;
}

/** One frame of the deck overview strip, in backing-canvas pixels. */
export interface StripFrame {
	/** Backing canvas width (CSS scales it to the deck's strip width). */
	widthPx: number;
	/** Backing canvas height. */
	heightPx: number;
	/** Track length; null on an empty deck. Every x mapping needs it. */
	durationMs: number | null;
	/** Analysis waveform; null when the track has no analysis. */
	waveform: StripWaveformBands | null;
	/** Validated vocals of the loaded analysis; null when absent. */
	vocals: StripVocals | null;
	/** issue #10238 `show_vocal_lane` off: no vocal bars on this strip. */
	hideVocals?: boolean;
	/** Engaged loop reported by the engine; null when no loop is running. */
	loop: LoopBandSource | null;
	/** Stored loop hot cues. They remain visible when no loop is engaged. */
	loopCues: readonly StripLoopCue[];
	/** STRUCT-05: section bands behind the preview bars; null when the
	 * show_sections pref is off or there is nothing to paint. Labels are the
	 * caller's DOM (this canvas is stretched non-uniformly, text would smear).
	 * `drawBands` is the lazily loaded painter (sections-lazy.svelte.ts), passed
	 * in so this module never imports sections.ts into the "/" bundle. */
	sections?: {
		spans: readonly SectionSpan[];
		palette: SectionPalette;
		drawBands: SectionsApi['drawSectionBands'];
	} | null;
	waveformDesign?: WaveformDesign;
	/** Band fills from `resolveStripBandColors`; default rekordbox 3Band. */
	bandColors?: WaveBandColors;
	/** WAVE-EQ-5230: live EQ/filter band gains the engine reports for this deck;
	 * null or absent draws the analysis unscaled. */
	bandGains?: BandGains | null;
	/** Tempo-stability answer; null when not fetched, not analyzed, or the
	 * show_tempo_lane pref is off. A steady track has no segments and paints nothing. */
	tempo?: TempoCurveData | null;
	tempoColors?: TempoLaneColors;
	tempoScale?: TempoLaneScale;
	/** CSS width / height stretch of one backing px (icons stay round); default 1. */
	tempoXStretch?: number;
}

/** Unity gains: what an unreported deck, or a heights-only payload, draws at. */
const UNITY: BandGains = Object.freeze({ low: 1, mid: 1, high: 1 });

/**
 * Paint one full strip frame. The canvas is cleared first, so this is the
 * single entry point - callers never draw layers themselves.
 */
/** Paint normalized preview bands only (library mini-strip + deck strip waveform). */
export function drawStripPreviewBands(
	ctx: CanvasRenderingContext2D,
	bands: StripBands,
	payloadKind: 'tri' | 'mono',
	widthPx: number,
	heightPx: number,
	design: WaveformDesign = 'tri-band',
	colors: WaveBandColors = DEFAULT_BAND_COLORS,
	gains: BandGains | null = null
): void {
	const kind = resolveStripWaveformKind(payloadKind, design);
	// A 'mono' payload carries heights only: there is no band to scale.
	const applied = gains === null || payloadKind === 'mono' ? UNITY : gains;
	_drawPreview(ctx, bands, kind, widthPx, heightPx, design, colors, applied);
}

export function drawStripWaveform(ctx: CanvasRenderingContext2D, frame: StripFrame): void {
	const { widthPx: w, heightPx: h, durationMs } = frame;
	ctx.clearRect(0, 0, w, h);
	if (frame.sections != null && durationMs !== null && durationMs > 0) {
		const { spans, palette, drawBands } = frame.sections;
		drawBands(ctx, spans, 0, w / (durationMs / 1000), w, h, palette, 0);
	}
	if (frame.waveform !== null) {
		drawStripPreviewBands(
			ctx,
			frame.waveform.preview,
			frame.waveform.kind,
			w,
			h,
			frame.waveformDesign ?? 'tri-band',
			frame.bandColors ?? DEFAULT_BAND_COLORS,
			frame.bandGains ?? null
		);
		if (frame.hideVocals !== true && frame.vocals !== null && durationMs !== null && durationMs > 0) {
			_drawVocalBars(ctx, frame.vocals, durationMs, w, (frame.bandColors ?? DEFAULT_BAND_COLORS).vocal);
		}
	}
	// The loop band is NOT gated on analysis: it overlays the strip rect
	// itself, so an engaged loop stays visible on a track whose ANLZ is
	// missing. An engaged loop is never invisible on a surface that has a
	// duration to place it with (DECKUX-04).
	if (frame.tempo != null && durationMs !== null && durationMs > 0) {
		const scale = h / TEMPO_CURVE_REFERENCE_HEIGHT_PX;
		drawTempoCurve(
			ctx,
			frame.tempo,
			0,
			w / (durationMs / 1000),
			w,
			{
				top: (VOCAL_BAR_BACKING_PX + 1) * scale,
				height: h / 3,
				lineWidth: TEMPO_CURVE_LINE_PX * scale,
				iconPx: TEMPO_CURVE_ICON_PX * scale,
				xStretch: frame.tempoXStretch ?? 1
			},
			frame.tempoColors ?? TEMPO_LANE_COLORS.dark,
			frame.tempoScale ?? 'fixed_10'
		);
	}
	if (durationMs !== null && durationMs > 0) {
		drawLoopRegion(ctx, frame.loop, (ms) => (ms / durationMs) * w, w, h);
		_drawLoopCueBands(ctx, frame.loopCues, durationMs, w, h);
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
	bands: StripBands,
	kind: 'tri' | 'mono',
	widthPx: number,
	heightPx: number,
	design: WaveformDesign,
	colors: WaveBandColors,
	g: BandGains
): void {
	const n = bands.length;
	if (n === 0) return;
	const w = widthPx / n;
	if (design === 'blocks') {
		_drawBlocks(ctx, bands, kind, widthPx, heightPx, colors, g);
		return;
	}
	if (design === 'line') {
		ctx.strokeStyle = colors.mono;
		ctx.lineWidth = heightPx / STRIP_MIN_BACKING.heightPx;
		ctx.beginPath();
		for (let i = 0; i < n; i++) {
			const x = i * w + w / 2;
			const v = Math.max(bands.low[i] * g.low, bands.mid[i] * g.mid, bands.high[i] * g.high);
			const y = heightPx - Math.min(1, v) * heightPx;
			if (i === 0) ctx.moveTo(x, y);
			else ctx.lineTo(x, y);
		}
		ctx.stroke();
		return;
	}
	for (let i = 0; i < n; i++) {
		const x = i * w;
		if (kind === 'tri' && design === 'rgb') {
			const lo = bands.low[i] * g.low;
			const mi = bands.mid[i] * g.mid;
			const hi = bands.high[i] * g.high;
			const color = rgbColumnColor({ low: lo, mid: mi, high: hi });
			if (color !== null) _bar(ctx, x, w, Math.max(lo, mi, hi), color, heightPx);
		} else if (kind === 'tri') {
			_bar(ctx, x, w, bands.low[i] * g.low, colors.low, heightPx);
			_bar(ctx, x, w, bands.mid[i] * g.mid, colors.mid, heightPx);
			_bar(ctx, x, w, bands.high[i] * g.high, colors.high, heightPx);
		} else {
			// mono = heights only; single color, never synthesized bands.
			const v = Math.max(bands.low[i] * g.low, bands.mid[i] * g.mid, bands.high[i] * g.high);
			_bar(ctx, x, w, v, colors.mono, heightPx);
		}
	}
}

function _drawVocalBars(
	ctx: CanvasRenderingContext2D,
	vocals: StripVocals,
	durationMs: number,
	widthPx: number,
	vocalColor: string
): void {
	// rekordbox + demucs render identically; barless states draw nothing.
	if (vocals.status !== 'rekordbox' && vocals.status !== 'demucs') return;
	ctx.fillStyle = vocalColor;
	for (const region of vocals.regions) {
		const x0 = Math.max(0, ((region.start_s * 1000) / durationMs) * widthPx);
		const x1 = Math.min(widthPx, ((region.end_s * 1000) / durationMs) * widthPx);
		if (x1 <= x0) continue;
		ctx.globalAlpha = vocalAlpha(region.intensity);
		ctx.fillRect(x0, 0, x1 - x0, VOCAL_BAR_BACKING_PX);
	}
	ctx.globalAlpha = 1;
}

function _drawLoopCueBands(
	ctx: CanvasRenderingContext2D,
	loopCues: readonly StripLoopCue[],
	durationMs: number,
	widthPx: number,
	heightPx: number
): void {
	for (const cue of loopCues) {
		const band = loopBandPx({ ...cue, engaged: true }, (ms) => (ms / durationMs) * widthPx, widthPx);
		if (band === null) continue;
		// DECKUX-8337: sized for the 40 px-high buffer, scaled with the real one.
		const scale = heightPx / STRIP_MIN_BACKING.heightPx;
		const markerHeight = Math.min(LOOP_CUE_MARKER_PX * scale, heightPx);
		ctx.fillStyle = LOOP_CUE_COLOR;
		ctx.fillRect(band.left, 0, band.right - band.left, markerHeight);
		ctx.lineWidth = scale;
		ctx.strokeStyle = LOOP_CUE_OUTLINE;
		ctx.strokeRect(band.left + 0.5, 0.5, Math.max(0, band.right - band.left - 1), markerHeight - 1);
	}
}

/** 'blocks' design: one-sided bars growing up from the bottom baseline,
 * BLOCK_BAR_PX wide on a BLOCK_PITCH_PX pitch. 'tri' payloads stack the bands
 * like the tri strip (low behind, high in front, each its own height);
 * 'mono' payloads paint one color. */
function _drawBlocks(
	ctx: CanvasRenderingContext2D,
	bands: StripBands,
	kind: 'tri' | 'mono',
	widthPx: number,
	heightPx: number,
	colors: WaveBandColors,
	g: BandGains
): void {
	const n = bands.length;
	const layers: [string, (i: number) => number][] =
		kind === 'mono'
			? [[colors.mono, (i) => Math.max(bands.low[i] * g.low, bands.mid[i] * g.mid, bands.high[i] * g.high)]]
			: [
					[colors.low, (i) => bands.low[i] * g.low],
					[colors.mid, (i) => bands.mid[i] * g.mid],
					[colors.high, (i) => bands.high[i] * g.high]
				];
	for (const [color, valueAt] of layers) {
		ctx.fillStyle = color;
		for (let x = 0; x < widthPx; x += BLOCK_PITCH_PX) {
			const p0 = Math.floor((x / widthPx) * n);
			const p1 = Math.min(n - 1, Math.max(p0, Math.ceil(((x + BLOCK_PITCH_PX) / widthPx) * n) - 1));
			let v = 0;
			for (let i = p0; i <= p1; i++) v = Math.max(v, valueAt(i));
			const barH = Math.round(Math.max(0, Math.min(1, v)) * heightPx);
			if (barH > 0) ctx.fillRect(x, heightPx - barH, BLOCK_BAR_PX, barH);
		}
	}
}

//-----------------------------------------------------------------------------
// DECKUX-8337: backing store sized to the strip, not a fixed 400x40
//-----------------------------------------------------------------------------

/** The strip's drawn size before layout has measured it, and its floor. */
export const STRIP_MIN_BACKING = { widthPx: 400, heightPx: 40 } as const;

/** Cap per side: a 5K ultrawide at 2x stays sharp, and one repaint on resize stays cheap. */
export const STRIP_MAX_BACKING_PX = 4096;

/**
 * Device pixels the strip canvas draws at for a CSS box and pixel density.
 * A fixed 400x40 buffer stretched to a 900 px strip at 2x upscaled 4.5x and
 * pixelated on wide screens. Repaints only when this changes (resize, DPR).
 */
export function stripBackingSize(
	cssWidthPx: number,
	cssHeightPx: number,
	devicePixelRatio: number
): { widthPx: number; heightPx: number } {
	if (!(devicePixelRatio > 0)) throw new Error(`stripBackingSize: devicePixelRatio must be > 0, got ${devicePixelRatio}`);
	const side = (css: number, floor: number): number =>
		Math.min(STRIP_MAX_BACKING_PX, Math.max(floor, Math.round(Math.max(0, css) * devicePixelRatio)));
	return {
		widthPx: side(cssWidthPx, STRIP_MIN_BACKING.widthPx),
		heightPx: side(cssHeightPx, STRIP_MIN_BACKING.heightPx)
	};
}
