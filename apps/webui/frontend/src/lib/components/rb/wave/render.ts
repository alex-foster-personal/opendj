/**
 * Pure canvas painters for the wavestack rows (build unit: wavestack,
 * COMPONENT-MAP 1.2 / SCREENSHOT-SPEC 2).
 *
 * The caller owns the canvas, DPR transform and the rAF policy; every
 * function here is a stateless painter working in CSS-pixel space.
 * Colours come from the .perf-root CSS vars (theme.css) via readPalette -
 * no duplicated hex values.
 *
 * Rendering honesty: tri-band rows draw the three REAL band arrays; 'mono'
 * payloads draw a single-colour waveform from the per-point max of whatever
 * band arrays the server filled - bands are NEVER synthesised.
 */
import { vocalsOf } from '$lib/rb/api-rb';
import { shouldPaintBeatGrid } from '$lib/player/grid-features';
import type { AnlzBeat, AnlzData, AnlzTempoChange, AnlzWaveform } from '$lib/rb/anlz-types';
import type { LoopState } from '$lib/rb/deck-state-types';
import { LOOP_MIN_BAND_PX, loopBandPx, visibleBeatLines, type LoopBandSource } from './wave-math';
import {
	drawLoopCueBands,
	drawPhraseMarkers,
	drawPointCueMarkers,
	MARKER_BAND_PX,
	type WavePalette
} from './cues';

// Cue-marker painting, the wavestack palette and its WCAG contrast floor
// live in ./cues (issue #877) - readPalette/WavePalette re-exported here so
// WaveRow.svelte keeps its one import path; contrastRatio/CUE_MIN_CONTRAST/
// relativeLuminance have no caller outside ./cues itself, so they are not
// re-exported (wave-cue-contrast.test.mjs loads ./cues directly).
export { readPalette } from './cues';
export type { WavePalette } from './cues';

/** Vocal-region bar colour (SPIKE-B1 blue bars). A literal on purpose:
 * theme.css belongs to the shared theme unit and the canvas painters
 * already mirror colours as literals where a var cannot be read cheaply.
 * Shared by the wavestack rows, deck strip and browser preview strips. */
export const VOCAL_BLUE = '#4fb2ff';

/** Height of the vocal bar layer in CSS px ('2px-ish' per requirement). */
export const VOCAL_BAR_PX = 2;

/** Engaged-loop band colour, as bare `r, g, b` so callers can pick an alpha.
 * Shared by every waveform surface so one loop reads the same on the
 * wavestack row and on the deck's overview strip (DECKUX-04). */
export const LOOP_ORANGE_RGB = '232, 161, 58';

/** Translucent body of the loop band; the waveform stays readable under it. */
export const LOOP_FILL_ALPHA = 0.42;

/** Near-solid in/out edges so the loop boundaries are unambiguous. */
export const LOOP_EDGE_ALPHA = 0.95;

/** Widest in/out edge in surface px. Narrow bands get proportionally
 * thinner edges (see drawLoopRegion) so a short loop stays a band, not
 * two overlapping edges. */
const LOOP_EDGE_MAX_PX = 3;

/** PVDI region intensity (1..4, max-in-run per SPIKE-B1) -> bar opacity,
 * ramp 0.5 -> 1.0 so stronger vocal passages read stronger. Real data
 * only: callers must never invoke this without a rekordbox-status region. */
export function vocalAlpha(intensity: number): number {
	const i = Math.max(1, Math.min(4, Math.round(intensity)));
	return 0.5 + ((i - 1) * 0.5) / 3;
}

/** Seconds of track visible across one row (window is centered on the
 * fixed playhead). 24s keeps the <=38400-point detail waveform dense. */
export const WAVE_WINDOW_S = 24;

/** Playhead is pure white in the screenshot; not a themed surface colour. */
/** Center 'now' line - red by default; Beat Sync followers override via tone. */
const PLAYHEAD_COLORS = {
	stopped: '#fff',
	now: '#e23a32',
	master: '#e0cc6e',
	bar1: '#35c04f',
	synced: '#7ed992',
	drift: '#ff2d2d'
} as const;

export type PlayheadTone = keyof typeof PLAYHEAD_COLORS | 'masterSynced';

/** Rendering style only (matches rekordbox's white core): highs are drawn
 * at reduced height so the white band reads as the inner core. The band
 * DATA is untouched. */
const HIGH_BAND_SCALE = 0.6;
const MID_BAND_SCALE = 0.85;

/** Perceptual amplitude shaping (rendering only, the band DATA is never
 * modified). Raw PWV6/PWV7 bytes sit mostly in the 0.3-0.7 range after
 * /127 scaling, which painted linearly reads as a thin ribbon in a 40px
 * row; rekordbox draws visibly denser. Two standard display steps:
 *   1. per-track per-band normalization to the band's own 99th percentile
 *      (loud parts of THIS track reach full height; relative dynamics -
 *      quiet intros vs drops - are preserved);
 *   2. gamma lift v^0.45 to widen the mid range.
 * Zero stays zero; nothing is drawn where the analysis is silent. */
const AMP_GAMMA = 0.45;
/** Floor for the p99 divisor so near-silent bands cannot blow up noise. */
const NORM_FLOOR = 0.1;

interface BandNorms {
	low: number;
	mid: number;
	high: number;
}

/** Per-waveform normalization cache - computed once per anlz payload. */
const _normCache = new WeakMap<AnlzWaveform, BandNorms>();

interface WaveBandImage {
	canvas: HTMLCanvasElement;
	key: string;
}

/**
 * Band geometry is the expensive part of a scrolling waveform. It changes only
 * with the immutable waveform data, zoom, row width, height, or palette, so
 * retain an offscreen full-track image and translate it under the fixed
 * playhead. The image is deliberately transparent: moving overlays remain on
 * the destination canvas and no cached background can obscure them.
 */
/** Test-only cache reset. The production cache is weakly owned by ANLZ data. */
export function resetWaveBandCacheForTest(): void {
	// WeakMap has no clear(), so replace it for deterministic isolated tests.
	_bandImages = new WeakMap<AnlzWaveform, WaveBandImage>();
}

let _bandImages = new WeakMap<AnlzWaveform, WaveBandImage>();

function _p99(values: number[]): number {
	const nonZero = values.filter((v) => v > 0);
	if (nonZero.length === 0) return 1;
	const sorted = nonZero.slice().sort((a, b) => a - b);
	const p99 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.99))];
	return Math.min(1, Math.max(NORM_FLOOR, p99));
}

function _normsFor(waveform: AnlzWaveform): BandNorms {
	const cached = _normCache.get(waveform);
	if (cached !== undefined) return cached;
	const bands = waveform.detail;
	const norms: BandNorms = {
		low: _p99(bands.low),
		mid: _p99(bands.mid),
		high: _p99(bands.high)
	};
	_normCache.set(waveform, norms);
	return norms;
}

function _amp(v: number, norm: number): number {
	if (v <= 0) return 0;
	return Math.pow(Math.min(1, v / norm), AMP_GAMMA);
}

/** One frame's inputs. anlz null = loaded track without (or awaiting)
 * analysis: background + playhead only, never an invented waveform. */
export interface WaveRowFrame {
	widthCss: number;
	heightCss: number;
	positionMs: number;
	durationMs: number;
	anlz: AnlzData | null;
	palette: WavePalette;
	/** Playback-rate ratio (1 = unity). Warps the visible window into
	 * wall-clock seconds so beat-synced decks share grid spacing away from
	 * the playhead, not only at the center. */
	pitch: number;
	/** Engaged loop region (orange highlight); null when no active loop. */
	loop: LoopState | null;
	/** Beat Sync follower playhead tone; default `now` (red). */
	playheadTone?: PlayheadTone;
	/** Wall time for drift pulse animation. */
	playheadTimeMs?: number;
}

/** Paint one full row frame. ctx must already be DPR-scaled so all
 * coordinates here are CSS pixels. */
export function drawWaveRow(ctx: CanvasRenderingContext2D, frame: WaveRowFrame): void {
	const { widthCss: w, heightCss: h, palette } = frame;
	ctx.fillStyle = palette.bg;
	ctx.fillRect(0, 0, w, h);

	const pitch = frame.pitch;
	if (!Number.isFinite(pitch) || pitch <= 0) {
		throw new RangeError(`drawWaveRow: pitch must be finite and > 0, got ${pitch}`);
	}
	const durS = frame.durationMs / 1000;
	// WAVE_WINDOW_S is wall-clock; scale into track time via pitch so a
	// synced follower (pitch = masterBpm/nativeBpm) shows the same beat
	// spacing as the master across the whole row.
	const trackWindowS = WAVE_WINDOW_S * pitch;
	const tLeft = frame.positionMs / 1000 - trackWindowS / 2;
	const pxPerS = w / trackWindowS;

	if (frame.anlz !== null && durS > 0) {
		_drawCachedBands(ctx, frame.anlz.waveform, tLeft, pxPerS, durS, w, h, palette);
		drawLoopRegion(ctx, frame.loop, (ms) => (ms / 1000 - tLeft) * pxPerS, w, h);
		// Loop cue bands paint as background, before the beat grid/phrases they
		// would otherwise blank out for their span; point cue markers stay in
		// the foreground, after them (discussion_r3918219289).
		drawLoopCueBands(ctx, frame.anlz.cues, tLeft, pxPerS, w, palette);
		if (shouldPaintBeatGrid(frame.anlz)) {
			_drawBeatGrid(ctx, frame.anlz.beatgrid.beats, tLeft, pxPerS, w, h, palette);
			_drawTempoChanges(ctx, frame.anlz.tempo_changes, tLeft, pxPerS, w, h, palette);
		}
		drawPhraseMarkers(ctx, frame.anlz.phrases, tLeft, pxPerS, w, palette);
		drawPointCueMarkers(ctx, frame.anlz.cues, tLeft, pxPerS, w, palette);
		_drawVocals(ctx, frame.anlz, tLeft, pxPerS, w);
	}
	drawPlayhead(ctx, w, h, frame.playheadTone ?? 'now', frame.playheadTimeMs ?? 0);
}

export interface StemWaveRowFrame {
	envelope: Float32Array | readonly number[];
	/** Main-row scroll: track seconds x (width / (WAVE_WINDOW_S x pitch)). */
	scrollPx: number;
	/** Whole-track duration the envelope spans, in ms. */
	durationMs: number | null;
	/** Deck pitch, the same rate the main row scales its window by. */
	pitch: number;
	width: number;
	height: number;
	color: string;
}

/** Paint one stem mini-waveform row synced to the main wavestack scroll model.
 * The envelope spans the WHOLE track, so one point is durationS / points of
 * track time, drawn at the main row's px-per-second (Codex P1 on #3645). */
export function drawStemWaveRow(ctx: CanvasRenderingContext2D, frame: StemWaveRowFrame): void {
	const { envelope, scrollPx, durationMs, pitch, width, height, color } = frame;
	ctx.clearRect(0, 0, width, height);
	if (envelope.length === 0 || width <= 0 || height <= 0) return;
	if (durationMs === null || !(durationMs > 0) || !(pitch > 0)) return;

	const points = envelope.length;
	const pxPerS = width / (WAVE_WINDOW_S * pitch);
	const pxPerPoint = ((durationMs / 1000) * pxPerS) / points;
	const startPx = scrollPx - width / 2;

	ctx.fillStyle = color;
	for (let x = 0; x < width; x++) {
		const trackPx = startPx + x;
		const idx = Math.floor(trackPx / pxPerPoint);
		if (idx < 0 || idx >= points) continue;
		const amp = envelope[idx];
		if (!Number.isFinite(amp) || amp <= 0) continue;
		const barH = Math.max(1, Math.round(amp * (height - 1)));
		const y = Math.round((height - barH) / 2);
		ctx.fillRect(x, y, 1, barH);
	}
}

// ----------------------------------------------------------- _helpers

function _bucketMax(values: number[], p0: number, p1: number): number {
	let max = 0;
	for (let i = p0; i <= p1; i++) {
		const v = values[i];
		if (v > max) max = v;
	}
	return max;
}

function _mirrorRect(path: Path2D, x: number, centerY: number, halfHeight: number): void {
	path.rect(x, centerY - halfHeight, 1, halfHeight * 2);
}

function _drawCachedBands(
	ctx: CanvasRenderingContext2D,
	waveform: AnlzWaveform,
	tLeft: number,
	pxPerS: number,
	durS: number,
	w: number,
	h: number,
	palette: WavePalette
): void {
	// Node painter tests intentionally provide only Path2D. Browser production
	// always has document, while this direct branch keeps those geometry tests
	// exercising the same real bucket painter without a fake DOM canvas.
	if (typeof document === 'undefined') {
		_drawBands(ctx, waveform, pxPerS, durS, w, h, palette);
		return;
	}
	const key = `${pxPerS}:${w}:${h}:${palette.low}:${palette.mid}:${palette.high}`;
	let image = _bandImages.get(waveform);
	if (image === undefined || image.key !== key) {
		image = { canvas: _buildBandImage(waveform, pxPerS, durS, h, palette), key };
		_bandImages.set(waveform, image);
	}
	ctx.drawImage(image.canvas, -tLeft * pxPerS, 0);
}

function _buildBandImage(
	waveform: AnlzWaveform,
	pxPerS: number,
	durS: number,
	h: number,
	palette: WavePalette
): HTMLCanvasElement {
	if (typeof document === 'undefined') {
		throw new Error('wave band cache requires a browser canvas');
	}
	const canvas = document.createElement('canvas');
	const w = Math.max(1, Math.ceil(durS * pxPerS));
	canvas.width = w;
	canvas.height = h;
	const ctx = canvas.getContext('2d');
	if (ctx === null) throw new Error('wave band cache: 2d context unavailable');
	_drawBands(ctx, waveform, pxPerS, durS, w, h, palette);
	return canvas;
}

/** Build the transparent full-track band image once, never per animation frame. */
function _drawBands(
	ctx: CanvasRenderingContext2D,
	waveform: AnlzWaveform,
	pxPerS: number,
	durS: number,
	w: number,
	h: number,
	palette: WavePalette
): void {
	const bands = waveform.detail;
	const n = bands.length;
	if (n === 0) return;
	const centerY = MARKER_BAND_PX + (h - MARKER_BAND_PX) / 2;
	const halfH = (h - MARKER_BAND_PX) / 2 - 1;
	const mono = waveform.kind === 'mono';
	const norms = _normsFor(waveform);
	// Mono payloads mix all three arrays into one height, so normalize by
	// the loudest band's p99 rather than any single band's.
	const monoNorm = Math.max(norms.low, norms.mid, norms.high);

	const lowPath = new Path2D();
	const midPath = new Path2D();
	const highPath = new Path2D();

	for (let x = 0; x < w; x++) {
		const p0 = Math.max(0, Math.floor((x / w) * n));
		const p1 = Math.min(n - 1, Math.max(p0, Math.ceil(((x + 1) / w) * n) - 1));
		if (mono) {
			// Heights only (PWAV/PWV3): the contract does not pin which band
			// array carries them, so take the per-point max across all three.
			const v = _amp(
				Math.max(
					_bucketMax(bands.low, p0, p1),
					_bucketMax(bands.mid, p0, p1),
					_bucketMax(bands.high, p0, p1)
				),
				monoNorm
			);
			if (v > 0) _mirrorRect(lowPath, x, centerY, v * halfH);
			continue;
		}
		const lo = _amp(_bucketMax(bands.low, p0, p1), norms.low);
		const mi = _amp(_bucketMax(bands.mid, p0, p1), norms.mid);
		const hi = _amp(_bucketMax(bands.high, p0, p1), norms.high);
		if (lo > 0) _mirrorRect(lowPath, x, centerY, lo * halfH);
		if (mi > 0) _mirrorRect(midPath, x, centerY, mi * halfH * MID_BAND_SCALE);
		if (hi > 0) _mirrorRect(highPath, x, centerY, hi * halfH * HIGH_BAND_SCALE);
	}

	if (mono) {
		// Single-colour waveform - never synthesised tri-bands.
		ctx.fillStyle = palette.mid;
		ctx.fill(lowPath);
		return;
	}
	ctx.fillStyle = palette.low;
	ctx.fill(lowPath);
	ctx.globalAlpha = 0.9;
	ctx.fillStyle = palette.mid;
	ctx.fill(midPath);
	ctx.fillStyle = palette.high;
	ctx.fill(highPath);
	ctx.globalAlpha = 1;
}

function _drawTempoChanges(
	ctx: CanvasRenderingContext2D,
	changes: readonly AnlzTempoChange[] | undefined,
	tLeft: number,
	pxPerS: number,
	w: number,
	h: number,
	palette: WavePalette
): void {
	if (changes === undefined || changes.length === 0) return;
	ctx.strokeStyle = palette.phrase;
	ctx.lineWidth = 1;
	for (const change of changes) {
		const x = (change.at_s - tLeft) * pxPerS;
		if (x < -2 || x > w + 2) continue;
		ctx.beginPath();
		ctx.moveTo(x, 0);
		ctx.lineTo(x, h);
		ctx.stroke();
	}
}

function _drawBeatGrid(
	ctx: CanvasRenderingContext2D,
	beats: AnlzBeat[],
	tLeft: number,
	pxPerS: number,
	w: number,
	h: number,
	palette: WavePalette
): void {
	ctx.fillStyle = palette.tick;
	for (const line of visibleBeatLines(beats, tLeft, pxPerS, w, h)) {
		// The low-alpha grid remains visible through the waveform. The original
		// 8px/4px beat caps are then repainted at their stronger alpha.
		ctx.globalAlpha = line.alpha;
		ctx.fillRect(line.x, line.y, line.width, line.height);
		ctx.globalAlpha = line.capAlpha;
		ctx.fillRect(line.x, line.y, line.width, line.capHeight);
	}
	ctx.globalAlpha = 1;
}

/**
 * Semi-transparent orange band over an engaged loop (Rekordbox parity).
 *
 * Surface-agnostic on purpose: `toPx` maps a track time in ms to this
 * surface's x, so the wavestack row (scrolling 24s window) and the deck
 * overview strip (whole track) share one painter and one visual language.
 * A no-op when no loop is engaged, so callers never need their own guard.
 */
export function drawLoopRegion(
	ctx: CanvasRenderingContext2D,
	loop: LoopBandSource | null,
	toPx: (ms: number) => number,
	widthPx: number,
	heightPx: number
): void {
	const band = loopBandPx(loop, toPx, widthPx, LOOP_MIN_BAND_PX);
	if (band === null) return;
	const { left, right } = band;
	ctx.fillStyle = `rgba(${LOOP_ORANGE_RGB}, ${LOOP_FILL_ALPHA})`;
	ctx.fillRect(left, 0, right - left, heightPx);
	// Edges never exceed half the band, so a narrow loop reads as one solid
	// tick rather than two edges overlapping into a wider band than it is.
	const edge = Math.max(1, Math.min(LOOP_EDGE_MAX_PX, (right - left) / 2));
	ctx.fillStyle = `rgba(${LOOP_ORANGE_RGB}, ${LOOP_EDGE_ALPHA})`;
	ctx.fillRect(left, 0, edge, heightPx);
	ctx.fillRect(right - edge, 0, edge, heightPx);
}

function _drawVocals(
	ctx: CanvasRenderingContext2D,
	anlz: AnlzData,
	tLeft: number,
	pxPerS: number,
	w: number
): void {
	// Four mandatory states (SPIKE-B1/B2): 'rekordbox' and 'demucs' draw
	// bars identically (demucs intensity is confidence on the same 1..4
	// ramp); no_vocals / not_analyzed draw NOTHING here (WaveRow surfaces
	// them as tooltips). vocalsOf throws on a malformed payload - a
	// contract breach must never render as 'no vocals'.
	const vocals = vocalsOf(anlz);
	if (vocals.status !== 'rekordbox' && vocals.status !== 'demucs') return;
	ctx.fillStyle = VOCAL_BLUE;
	for (const region of vocals.regions) {
		const x0 = Math.max(0, (region.start_s - tLeft) * pxPerS);
		const x1 = Math.min(w, (region.end_s - tLeft) * pxPerS);
		if (x1 <= x0) continue; // fully outside the window
		ctx.globalAlpha = vocalAlpha(region.intensity);
		ctx.fillRect(x0, 0, x1 - x0, VOCAL_BAR_PX);
	}
	ctx.globalAlpha = 1;
}

/**
 * CH3/4 still get a lighter fill than --rb-bg so the opaque canvas matches
 * the gutter; the colour now comes from --rb-waverow-secondary, not a
 * hardcoded dark hex. CSS alone cannot show through.
 */
export function resolvePaintPalette(deckId: number, palette: WavePalette): WavePalette {
	if (deckId !== 3 && deckId !== 4) return palette;
	return palette.secondaryBg === palette.bg
		? palette
		: { ...palette, bg: palette.secondaryBg };
}

/** Fixed center playhead. Always drawn (busy waveforms + empty decks).
 * Beat Sync followers pass bar1 / synced / drift; others keep `now` (red). */
export function drawPlayhead(
	ctx: CanvasRenderingContext2D,
	w: number,
	h: number,
	tone: PlayheadTone = 'now',
	timeMs: number = 0
): void {
	const centerX = Math.round(w / 2);
	if (tone === 'masterSynced') {
		// Two adjacent cores are deliberate: yellow says MASTER, while green
		// retains its established meaning, Beat Sync is engaged. Their matching
		// 3px glows / 1px cores keep the original equal-weight contract.
		// Both translucent glows must land before either opaque core. Painting
		// green's glow after yellow's core visibly contaminates the yellow core.
		_drawPlayheadGlow(ctx, PLAYHEAD_COLORS.master, centerX - 1, h);
		_drawPlayheadGlow(ctx, PLAYHEAD_COLORS.synced, centerX, h);
		_drawPlayheadCore(ctx, PLAYHEAD_COLORS.master, centerX - 1, h);
		_drawPlayheadCore(ctx, PLAYHEAD_COLORS.synced, centerX, h);
		return;
	}
	const color = PLAYHEAD_COLORS[tone];
	// One weight for every tone. The geometry was always identical, but the
	// glow alpha ran 0.32 (synced) to 0.55 (bar1), and a dimmer line at the
	// same width reads as a THINNER line - which is why pin 4a1e7e603f49
	// reported this as a width bug when nothing was ever a different width.
	// The tone carries its meaning in the colour; making it carry meaning in
	// the weight as well meant neither read cleanly.
	let glow = 0.45;
	let core = 1;
	if (tone === 'drift') {
		// Bright pulsing red - light-touch warning, still unmissable. The
		// exception that stays: a pulse is a change over time, not a
		// permanently different weight.
		const pulse = 0.55 + 0.45 * (0.5 + 0.5 * Math.sin(timeMs / 160));
		glow = 0.45 * pulse;
		core = pulse;
	}
	ctx.fillStyle = color;
	ctx.globalAlpha = glow;
	ctx.fillRect(centerX - 1, 0, 3, h);
	ctx.globalAlpha = core;
	ctx.fillRect(centerX, 0, 1, h);
	ctx.globalAlpha = 1;
}

function _drawPlayheadGlow(
	ctx: CanvasRenderingContext2D,
	color: string,
	coreX: number,
	h: number
): void {
	ctx.fillStyle = color;
	ctx.globalAlpha = 0.45;
	ctx.fillRect(coreX - 1, 0, 3, h);
	ctx.globalAlpha = 1;
}

function _drawPlayheadCore(
	ctx: CanvasRenderingContext2D,
	color: string,
	coreX: number,
	h: number
): void {
	ctx.fillStyle = color;
	ctx.globalAlpha = 1;
	ctx.fillRect(coreX, 0, 1, h);
	ctx.globalAlpha = 1;
}
