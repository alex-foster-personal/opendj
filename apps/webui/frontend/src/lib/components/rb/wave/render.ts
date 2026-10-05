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
import type { WaveformDesign } from '$lib/rb/waveform-design';
import { LOOP_MIN_BAND_PX, loopBandPx, visibleBeatLines, type LoopBandSource } from './wave-math';
import {
	drawLoopCueBands,
	drawPhraseMarkers,
	drawPointCueMarkers,
	MARKER_BAND_PX,
	type WavePalette
} from './cues';
import {
	drawGhostSeekPlayhead,
	drawMasterDownbeatOverlay,
	drawPlayheadWithColors,
	type MasterDownbeatOverlay
} from './wave-playhead-render';

/** Beat Sync follower playhead colours, keyed by PlayheadTone. Declared here,
 * not in wave-playhead-render.ts: that module draws with whatever colour map
 * it is handed (drawPlayheadWithColors) precisely so it never imports back
 * from this file - a real frontend.import_cycles regression, not a style
 * preference. This is also why this module's own source text still carries
 * the literal tests/unit/stopped-deck-presentation.test.mjs reads for the
 * stopped-deck-is-white regression. */
export const PLAYHEAD_COLORS = {
	stopped: '#fff',
	now: '#e23a32',
	master: '#e0cc6e',
	bar1: '#35c04f',
	synced: '#7ed992',
	drift: '#ff2d2d'
} as const;

export type PlayheadTone = keyof typeof PLAYHEAD_COLORS | 'masterSynced';

export function drawPlayhead(
	ctx: CanvasRenderingContext2D,
	w: number,
	h: number,
	tone: PlayheadTone = 'now',
	timeMs: number = 0
): void {
	drawPlayheadWithColors(ctx, w, h, PLAYHEAD_COLORS, tone, timeMs);
}

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

/** Rendering style only (matches rekordbox's white core): highs are drawn
 * at reduced height so the white band reads as the inner core. The band
 * DATA is untouched. */
const HIGH_BAND_SCALE = 0.6;
const MID_BAND_SCALE = 0.85;

/** 'blocks' design geometry: BLOCK_BAR_PX-wide bars on a BLOCK_PITCH_PX
 * pitch, i.e. a 1px gap, ONE-SIDED: bars grow up from the bottom baseline.
 * Rendering style only, heights are the real data. */
export const BLOCK_BAR_PX = 2;
export const BLOCK_PITCH_PX = 3;

/** How a block's height is derived from its band data (skin preview):
 *  - max:      per-block max of all bands, gamma-lifted (the original).
 *  - kick:     LOW band dominant, mid/high minor, gamma > 1 (expands peaks).
 *  - contrast: stretched between the rolling min/max over ~1 beat, scaled
 *              by that window's max so quiet sections stay quiet.
 *  - onset:    rise above the rolling mean over ~1/4 beat, plus a floor.
 *  - blend:    mix(max, contrast, BLEND_CONTRAST): a small step from max
 *              that still separates neighboring beats. */
export type BlocksVariant = 'max' | 'kick' | 'contrast' | 'onset' | 'blend';

export const BLOCKS_CFG = {
	VARIANT_DEFAULT: 'blend' as BlocksVariant,
	/** 0.25: whole-track neighbor variance 2.56x max on Strobe (contrast alone was 12x). */
	BLEND_CONTRAST: 0.25,
	KICK_LOW_WEIGHT: 0.8,
	KICK_GAMMA: 1.6,
	CONTRAST_WINDOW_BEATS: 1,
	ONSET_WINDOW_BEATS: 0.25,
	ONSET_FLOOR: 0.2,
	/** contrast/onset need a beat period; with no beat grid they paint as
	 * 'kick' (stated here, not hidden in the painter). */
	NO_GRID_VARIANT: 'kick' as BlocksVariant
} as const;

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

export function bandNormsFor(waveform: AnlzWaveform): BandNorms {
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
	/** DECKUX-20: user-selected paint style; default tri-band. */
	waveformDesign?: WaveformDesign;
	/** 'blocks' height model; default BLOCKS_CFG.VARIANT_DEFAULT. */
	blocksVariant?: BlocksVariant;
	/** DECKUX-21: master downbeat overlay while BeatSyncMax is on. */
	masterDownbeatOverlay?: MasterDownbeatOverlay | null;
	/** Pending deferred seek ghost playhead (ms). */
	ghostSeekMs?: number | null;
	ghostSeekVisible?: boolean;
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

	const design = frame.waveformDesign ?? 'tri-band';
	if (frame.anlz !== null && durS > 0) {
		const blocks: BlocksSpec = {
			variant: frame.blocksVariant ?? BLOCKS_CFG.VARIANT_DEFAULT,
			beatPeriodS: beatPeriodS(frame.anlz.beatgrid?.beats)
		};
		_drawCachedBands(ctx, frame.anlz.waveform, tLeft, pxPerS, durS, w, h, palette, design, blocks);
	}
	// Derived only from the trusted MASTER grid plus this deck's own position
	// and pitch, so a loaded row with no (or failed) local analysis still shows
	// the master's downbeats; painted after the bands, before everything else.
	if (durS > 0 && frame.masterDownbeatOverlay !== undefined && frame.masterDownbeatOverlay !== null) {
		drawMasterDownbeatOverlay(ctx, frame.masterDownbeatOverlay, tLeft, pxPerS, w, h);
	}
	if (frame.anlz !== null && durS > 0) {
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
		_drawVocals(ctx, frame.anlz, tLeft, pxPerS, w, frame.palette.vocal);
	}
	drawPlayhead(ctx, w, h, frame.playheadTone ?? 'now', frame.playheadTimeMs ?? 0);
	if (frame.ghostSeekMs !== undefined && frame.ghostSeekMs !== null && frame.ghostSeekVisible === true) {
		drawGhostSeekPlayhead(ctx, frame.ghostSeekMs, tLeft, pxPerS, w, h);
	}
}

export function resolveStripWaveformKind(
	waveformKind: 'tri' | 'mono',
	design: WaveformDesign
): 'tri' | 'mono' {
	if (design === 'mono') return 'mono';
	return waveformKind;
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
	palette: WavePalette,
	design: WaveformDesign,
	blocks: BlocksSpec
): void {
	// Node painter tests intentionally provide only Path2D. Browser production
	// always has document, while this direct branch keeps those geometry tests
	// exercising the same real bucket painter without a fake DOM canvas.
	if (typeof document === 'undefined') {
		_drawBands(ctx, waveform, pxPerS, durS, w, h, palette, design, blocks);
		return;
	}
	const key = `${design}:${blocks.variant}:${blocks.beatPeriodS}:${pxPerS}:${w}:${h}:${palette.low}:${palette.mid}:${palette.high}:${palette.mono}`;
	let image = _bandImages.get(waveform);
	if (image === undefined || image.key !== key) {
		image = { canvas: _buildBandImage(waveform, pxPerS, durS, h, palette, design, blocks), key };
		_bandImages.set(waveform, image);
	}
	ctx.drawImage(image.canvas, -tLeft * pxPerS, 0);
}

function _buildBandImage(
	waveform: AnlzWaveform,
	pxPerS: number,
	durS: number,
	h: number,
	palette: WavePalette,
	design: WaveformDesign,
	blocks: BlocksSpec
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
	_drawBands(ctx, waveform, pxPerS, durS, w, h, palette, design, blocks);
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
	palette: WavePalette,
	design: WaveformDesign,
	blocks: BlocksSpec = { variant: BLOCKS_CFG.VARIANT_DEFAULT, beatPeriodS: null }
): void {
	const bands = waveform.detail;
	const n = bands.length;
	if (n === 0) return;
	const centerY = MARKER_BAND_PX + (h - MARKER_BAND_PX) / 2;
	const halfH = (h - MARKER_BAND_PX) / 2 - 1;
	const mono = design === 'mono' || waveform.kind === 'mono';
	const line = design === 'line';
	const norms = bandNormsFor(waveform);
	// Mono payloads mix all three arrays into one height, so normalize by
	// the loudest band's p99 rather than any single band's.
	const monoNorm = Math.max(norms.low, norms.mid, norms.high);

	if (design === 'blocks') {
		const blocksPerBeat =
			blocks.beatPeriodS === null ? null : (blocks.beatPeriodS * pxPerS) / BLOCK_PITCH_PX;
		const heights = blockHeights(bands, w, norms, blocks.variant, blocksPerBeat);
		paintStackedBlocks(ctx, heights, mono ? null : blockBandShares(bands, w, norms), h, h - MARKER_BAND_PX - 1, palette);
		return;
	}

	const lowPath = new Path2D();
	const midPath = new Path2D();
	const highPath = new Path2D();
	const linePath = new Path2D();
	let lineStarted = false;

	for (let x = 0; x < w; x++) {
		const p0 = Math.max(0, Math.floor((x / w) * n));
		const p1 = Math.min(n - 1, Math.max(p0, Math.ceil(((x + 1) / w) * n) - 1));
		if (line) {
			const v = _amp(
				Math.max(
					_bucketMax(bands.low, p0, p1),
					_bucketMax(bands.mid, p0, p1),
					_bucketMax(bands.high, p0, p1)
				),
				monoNorm
			);
			const yTop = centerY - v * halfH;
			const yBot = centerY + v * halfH;
			if (!lineStarted) {
				linePath.moveTo(x, yTop);
				lineStarted = true;
			} else {
				linePath.lineTo(x, yTop);
			}
			linePath.lineTo(x, yBot);
			continue;
		}
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

	if (line) {
		ctx.strokeStyle = palette.mono;
		ctx.lineWidth = 1.5;
		ctx.stroke(lowPath);
		return;
	}
	if (mono) {
		// Single-colour waveform - never synthesised tri-bands.
		ctx.fillStyle = palette.mono;
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

interface BlocksSpec {
	variant: BlocksVariant;
	beatPeriodS: number | null;
}

/** Median beat period of the grid in seconds; null without a usable grid. */
export function beatPeriodS(beats: readonly AnlzBeat[] | undefined): number | null {
	if (beats === undefined || beats.length < 2) return null;
	const gaps: number[] = [];
	for (let i = 1; i < beats.length; i++) {
		const gap = beats[i].t - beats[i - 1].t;
		if (gap > 0) gaps.push(gap);
	}
	if (gaps.length === 0) return null;
	gaps.sort((a, b) => a - b);
	return gaps[Math.floor(gaps.length / 2)];
}

function _rollingWindow(values: Float32Array, i: number, half: number): [number, number] {
	return [Math.max(0, i - half), Math.min(values.length - 1, i + half)];
}

/** Per-block heights 0..1 for the 'blocks' design over a widthPx surface.
 * Pure (exported for tests): band DATA is read, never modified. */
export function blockHeights(
	bands: AnlzWaveform['detail'],
	widthPx: number,
	norms: { low: number; mid: number; high: number },
	variant: BlocksVariant,
	blocksPerBeat: number | null
): Float32Array {
	const n = bands.length;
	const count = Math.ceil(widthPx / BLOCK_PITCH_PX);
	const low = new Float32Array(count);
	const rest = new Float32Array(count);
	const all = new Float32Array(count);
	const monoNorm = Math.max(norms.low, norms.mid, norms.high);
	for (let b = 0; b < count; b++) {
		const x = b * BLOCK_PITCH_PX;
		const p0 = Math.max(0, Math.floor((x / widthPx) * n));
		const p1 = Math.min(n - 1, Math.max(p0, Math.ceil(((x + BLOCK_PITCH_PX) / widthPx) * n) - 1));
		const lo = _bucketMax(bands.low, p0, p1);
		const mi = _bucketMax(bands.mid, p0, p1);
		const hi = _bucketMax(bands.high, p0, p1);
		low[b] = Math.min(1, lo / norms.low);
		rest[b] = Math.min(1, Math.max(mi / norms.mid, hi / norms.high));
		all[b] = Math.min(1, Math.max(lo, mi, hi) / monoNorm);
	}
	const out = new Float32Array(count);
	const effective =
		blocksPerBeat === null && (variant === 'contrast' || variant === 'onset' || variant === 'blend')
			? BLOCKS_CFG.NO_GRID_VARIANT
			: variant;
	if (effective === 'max') {
		for (let b = 0; b < count; b++) out[b] = all[b] > 0 ? Math.pow(all[b], AMP_GAMMA) : 0;
	} else if (effective === 'blend') {
		const lifted = blockHeights(bands, widthPx, norms, 'max', blocksPerBeat);
		const stretched = blockHeights(bands, widthPx, norms, 'contrast', blocksPerBeat);
		const k = BLOCKS_CFG.BLEND_CONTRAST;
		for (let b = 0; b < count; b++) out[b] = (1 - k) * lifted[b] + k * stretched[b];
	} else if (effective === 'kick') {
		const w = BLOCKS_CFG.KICK_LOW_WEIGHT;
		for (let b = 0; b < count; b++) {
			out[b] = Math.pow(w * low[b] + (1 - w) * rest[b], BLOCKS_CFG.KICK_GAMMA);
		}
	} else if (effective === 'contrast') {
		const half = Math.max(1, Math.round(((blocksPerBeat as number) * BLOCKS_CFG.CONTRAST_WINDOW_BEATS) / 2));
		for (let b = 0; b < count; b++) {
			const [a, z] = _rollingWindow(all, b, half);
			let lo = 1;
			let hi = 0;
			for (let i = a; i <= z; i++) {
				if (all[i] < lo) lo = all[i];
				if (all[i] > hi) hi = all[i];
			}
			out[b] = hi - lo > 1e-6 ? ((all[b] - lo) / (hi - lo)) * hi : 0;
		}
	} else if (effective === 'onset') {
		const half = Math.max(1, Math.round(((blocksPerBeat as number) * BLOCKS_CFG.ONSET_WINDOW_BEATS) / 2));
		const rise = new Float32Array(count);
		let peak = 0;
		for (let b = 0; b < count; b++) {
			const [a, z] = _rollingWindow(all, b, half);
			let sum = 0;
			for (let i = a; i <= z; i++) sum += all[i];
			rise[b] = Math.max(0, all[b] - sum / (z - a + 1));
			if (rise[b] > peak) peak = rise[b];
		}
		const f = BLOCKS_CFG.ONSET_FLOOR;
		for (let b = 0; b < count; b++) {
			const r = peak > 0 ? rise[b] / peak : 0;
			out[b] = Math.min(1, (1 - f) * r + f * all[b]);
		}
	}
	return out;
}

/** Per-block share of each band relative to the block's loudest band (0..1),
 * so a stacked block keeps its total height and shows the band mix. */
export function blockBandShares(
	bands: AnlzWaveform['detail'],
	widthPx: number,
	norms: { low: number; mid: number; high: number }
): { low: Float32Array; mid: Float32Array; high: Float32Array } {
	const n = bands.length;
	const count = Math.ceil(widthPx / BLOCK_PITCH_PX);
	const out = { low: new Float32Array(count), mid: new Float32Array(count), high: new Float32Array(count) };
	for (let b = 0; b < count; b++) {
		const x = b * BLOCK_PITCH_PX;
		const p0 = Math.max(0, Math.floor((x / widthPx) * n));
		const p1 = Math.min(n - 1, Math.max(p0, Math.ceil(((x + BLOCK_PITCH_PX) / widthPx) * n) - 1));
		const lo = Math.min(1, _bucketMax(bands.low, p0, p1) / norms.low);
		const mi = Math.min(1, _bucketMax(bands.mid, p0, p1) / norms.mid);
		const hi = Math.min(1, _bucketMax(bands.high, p0, p1) / norms.high);
		const top = Math.max(lo, mi, hi);
		if (top <= 0) continue;
		out.low[b] = lo / top;
		out.mid[b] = mi / top;
		out.high[b] = hi / top;
	}
	return out;
}

/** Paint one-sided blocks. shares null = single mono color; else stacked
 * like tri-band: low (darkest) at the block height, mid and high in front at
 * their share of it (scaled like the tri-band core). */
export function paintStackedBlocks(
	ctx: CanvasRenderingContext2D,
	heights: Float32Array,
	shares: { low: Float32Array; mid: Float32Array; high: Float32Array } | null,
	baselineY: number,
	maxBarH: number,
	palette: Pick<WavePalette, 'low' | 'mid' | 'high' | 'mono'>
): void {
	const layers: [string, Float32Array | null, number][] =
		shares === null
			? [[palette.mono, null, 1]]
			: [
					[palette.low, shares.low, 1],
					[palette.mid, shares.mid, MID_BAND_SCALE],
					[palette.high, shares.high, HIGH_BAND_SCALE]
				];
	for (const [color, share, scale] of layers) {
		const path = new Path2D();
		for (let b = 0; b < heights.length; b++) {
			const frac = share === null ? 1 : share[b] * scale;
			const barH = Math.round(heights[b] * frac * maxBarH);
			if (barH > 0) path.rect(b * BLOCK_PITCH_PX, baselineY - barH, BLOCK_BAR_PX, barH);
		}
		ctx.fillStyle = color;
		ctx.fill(path);
	}
}

/** Mean squared difference between adjacent block heights: the 'can I see
 * individual beats' signal the variants are compared on. */
export function adjacentBlockVariance(heights: Float32Array): number {
	if (heights.length < 2) return 0;
	let sum = 0;
	for (let i = 1; i < heights.length; i++) sum += (heights[i] - heights[i - 1]) ** 2;
	return sum / (heights.length - 1);
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
	w: number,
	vocalColor: string
): void {
	// Four mandatory states (SPIKE-B1/B2): 'rekordbox' and 'demucs' draw
	// bars identically (demucs intensity is confidence on the same 1..4
	// ramp); no_vocals / not_analyzed draw NOTHING here (WaveRow surfaces
	// them as tooltips). vocalsOf throws on a malformed payload - a
	// contract breach must never render as 'no vocals'.
	const vocals = vocalsOf(anlz);
	if (vocals.status !== 'rekordbox' && vocals.status !== 'demucs') return;
	ctx.fillStyle = vocalColor;
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

/** Test seam: the uncached band painter (no DOM canvas needed). */
export const __test_drawBands = _drawBands;
