/**
 * WAVE-EQ-5230: the wavestack row's per-band image cache, so a deck's live EQ
 * and filter can rescale each ANLZ band at blit time.
 *
 * One transparent full-track image PER BAND (low, mid, high), keyed once per
 * waveform, zoom, width, design and palette (row height is reused by a
 * vertically scaled blit, PERF-PAINT-10529), and held as tiles of `tilePx`
 * columns built only when a visible span first needs them, like the combined
 * image (WAVEZOOM-01: a whole-track canvas at close zoom outgrows what
 * Chromium paints). A gain change then costs scaled blits and three
 * multiplies, never a geometry rebuild. Used only while the engine reports
 * gains; without them render.ts draws its single combined image as before.
 */
import type { AnlzWaveform } from '$lib/rb/anlz-types';
import type { BandGains } from '$lib/rb/wave-eq-gains.svelte';
import type { WaveHeight } from '$lib/rb/wave-height';
import type { WaveformDesign } from '$lib/rb/waveform-design';
import { MARKER_BAND_PX, type WavePalette } from './cues';
import {
	ampOf,
	bandHeightScale,
	bucketMax,
	HIGH_BAND_SCALE,
	MID_BAND_SCALE,
	mirrorRect,
	normsFor,
	upRect
} from './wave-amp';

type BandCanvases = readonly [HTMLCanvasElement, HTMLCanvasElement, HTMLCanvasElement];

/** One cached layer set; `pxPerS` is the px per track-second it was built at,
 * `fullW` the whole-track width in those px, `tiles` the built tile triples. */
export interface BandLayers {
	tiles: Map<number, BandCanvases>;
	fullW: number;
	tilePx: number;
	pxPerS: number;
	waveform: AnlzWaveform;
	palette: WavePalette;
	design: WaveformDesign;
	h: number;
	liveH: number;
	liveFrames: number;
	waveHeight: WaveHeight;
}

/** Height bookkeeping both band caches carry (PERF-PAINT-10529): the css px height
 * the tiles were built at, and the live height seen on the last draws. */
export interface BuiltHeight {
	h: number;
	liveH: number;
	liveFrames: number;
}

/** True when tiles built at `built.h` may be blitted, vertically scaled, at
 * `h`; false when the caller must rebuild at `h`. A move past `reuseRatio` of
 * the built height rebuilds at once; a height held for `settleFrames`
 * consecutive draws rebuilds so a settled row is pixel-exact again. Mutates the
 * live-height counters. */
export function keepBuiltHeight(
	built: BuiltHeight,
	h: number,
	reuseRatio: number,
	settleFrames: number
): boolean {
	if (h === built.h) {
		built.liveH = h;
		built.liveFrames = 0;
		return true;
	}
	if (built.h <= MARKER_BAND_PX || h <= MARKER_BAND_PX) return false;
	if (Math.abs(h - built.h) / built.h > reuseRatio) return false;
	built.liveFrames = h === built.liveH ? built.liveFrames + 1 : 1;
	built.liveH = h;
	return built.liveFrames < settleFrames;
}

/** Vertical scale of the waveform area (below the marker strip) from a tile
 * built `builtH` tall to a row `h` tall. */
export function bandHeightScaleFor(builtH: number, h: number): number {
	return (h - MARKER_BAND_PX) / (builtH - MARKER_BAND_PX);
}

interface WaveBandLayers {
	layers: BandLayers;
	key: string;
}

let _layerCache = new WeakMap<AnlzWaveform, WaveBandLayers>();

export const BAND_ORDER = ['low', 'mid', 'high'] as const;
type BandName = (typeof BAND_ORDER)[number];

/** Test-only: drop every cached layer set (render.ts resetWaveBandCacheForTest). */
export function resetBandLayerCache(): void {
	_layerCache = new WeakMap<AnlzWaveform, WaveBandLayers>();
}

export function newBandCanvas(
	w: number,
	h: number
): { canvas: HTMLCanvasElement; ctx: CanvasRenderingContext2D } {
	const canvas = document.createElement('canvas');
	canvas.width = w;
	canvas.height = h;
	const ctx = canvas.getContext('2d');
	if (ctx === null) throw new Error('wave band cache: 2d context unavailable');
	return { canvas, ctx };
}

/** The cached layer set for `key`, rekeyed on first use, when the key moved,
 * when `reusable` (render.ts bandImageScaleReusable, PERF-GRID-02) says the
 * live zoom drifted too far from the one it was built at, or when
 * keepBuiltHeight (PERF-PAINT-10529) says the row height moved past `heightReuse`
 * or settled. Tiles are built lazily by blitBandLayers. */
export function cachedBandLayers(
	waveform: AnlzWaveform,
	key: string,
	pxPerS: number,
	durS: number,
	h: number,
	palette: WavePalette,
	design: WaveformDesign,
	reusable: (builtPxPerS: number, livePxPerS: number) => boolean,
	tilePx: number,
	heightReuse: { ratio: number; settleFrames: number },
	waveHeight: WaveHeight = 'full'
): BandLayers {
	const hit = _layerCache.get(waveform);
	if (
		hit !== undefined &&
		hit.key === key &&
		reusable(hit.layers.pxPerS, pxPerS) &&
		keepBuiltHeight(hit.layers, h, heightReuse.ratio, heightReuse.settleFrames)
	)
		return hit.layers;
	const fullW = Math.max(1, Math.ceil(durS * pxPerS));
	const layers: BandLayers = {
		tiles: new Map(),
		fullW,
		tilePx,
		pxPerS,
		waveform,
		palette,
		design,
		h,
		liveH: h,
		liveFrames: 0,
		waveHeight
	};
	_layerCache.set(waveform, { layers, key });
	return layers;
}

function _bandTiles(layers: BandLayers, index: number): BandCanvases {
	const hit = layers.tiles.get(index);
	if (hit !== undefined) return hit;
	const x0 = index * layers.tilePx;
	const tileW = Math.min(layers.tilePx, layers.fullW - x0);
	const build = (band: BandName): HTMLCanvasElement => {
		const { canvas, ctx } = newBandCanvas(tileW, layers.h);
		drawBandLayer(
			ctx,
			layers.waveform,
			layers.fullW,
			layers.h,
			layers.palette,
			layers.design,
			band,
			1,
			x0,
			x0 + tileW,
			layers.waveHeight
		);
		return canvas;
	};
	const tiles: BandCanvases = [build('low'), build('mid'), build('high')];
	layers.tiles.set(index, tiles);
	return tiles;
}

/**
 * Blit the three cached band layers, each scaled vertically about the row's
 * centre line by its gain's height factor: 3 multiplies and 3 blits per frame,
 * no allocation, no geometry rebuild. A boosted band is clipped to the
 * waveform area, which is the same per-bar full-height cap the build applies.
 */
export function blitBandLayers(
	ctx: CanvasRenderingContext2D,
	layers: BandLayers,
	gains: BandGains,
	tLeft: number,
	pxPerS: number,
	w: number,
	h: number,
	design: WaveformDesign
): void {
	// Source columns in the layers' own scale: the visible `w` css px of track
	// starting at tLeft (scaled when a pitch nudge reused the set).
	const sx = tLeft * layers.pxPerS;
	const sw = (w * layers.pxPerS) / pxPerS;
	const sStart = Math.max(0, sx);
	const sEnd = Math.min(layers.fullW, sx + sw);
	if (!(sEnd > sStart)) return;
	const first = Math.floor(sStart / layers.tilePx);
	const last = Math.ceil(sEnd / layers.tilePx) - 1;
	const centerY = MARKER_BAND_PX + (h - MARKER_BAND_PX) / 2;
	const halfH = (h - MARKER_BAND_PX) / 2 - 1;
	const fullH = h - MARKER_BAND_PX - 1;
	const anchorY = layers.waveHeight === 'half' ? h : centerY;
	const sLow = bandHeightScale(gains.low);
	const sMid = bandHeightScale(gains.mid);
	const sHigh = bandHeightScale(gains.high);
	const k = bandHeightScaleFor(layers.h, h);
	const boosted = sLow > 1 || sMid > 1 || sHigh > 1;
	if (boosted) {
		ctx.save();
		ctx.beginPath();
		if (layers.waveHeight === 'half') ctx.rect(0, h - fullH, w, fullH);
		else ctx.rect(0, centerY - halfH, w, halfH * 2);
		ctx.clip();
	}
	// Solid one-pixel bars need no filtered resampling; nearest-neighbour keeps
	// a scaled blit as cheap as the raster allows.
	const smoothing = ctx.imageSmoothingEnabled;
	ctx.imageSmoothingEnabled = false;
	// Same layering and alpha as the combined image: lows opaque, then mids and
	// highs at 0.9 over them (mono: one colour, so a plain union).
	const alpha = design === 'mono' ? 1 : 0.9;
	for (let i = first; i <= last; i++) {
		const tiles = _bandTiles(layers, i);
		const x0 = i * layers.tilePx;
		const tsx = Math.max(sStart, x0);
		const tsw = Math.min(sEnd, x0 + tiles[0].width) - tsx;
		if (!(tsw > 0)) continue;
		const dx = ((tsx - sx) * w) / sw;
		const dw = (tsw * w) / sw;
		ctx.globalAlpha = 1;
		_blitLayer(ctx, tiles[0], sLow, tsx - x0, tsw, dx, dw, anchorY, k);
		ctx.globalAlpha = alpha;
		_blitLayer(ctx, tiles[1], sMid, tsx - x0, tsw, dx, dw, anchorY, k);
		_blitLayer(ctx, tiles[2], sHigh, tsx - x0, tsw, dx, dw, anchorY, k);
	}
	ctx.globalAlpha = 1;
	ctx.imageSmoothingEnabled = smoothing;
	if (boosted) ctx.restore();
}

function _blitLayer(
	ctx: CanvasRenderingContext2D,
	layer: HTMLCanvasElement,
	scale: number,
	sx: number,
	sw: number,
	dx: number,
	dw: number,
	anchorY: number,
	k: number
): void {
	if (scale === 0) return;
	// Source-rect form: name only the visible columns of this tile. Rows map by
	// the built height (k, PERF-PAINT-10529), then the gain scales about anchorY
	// (full row centre, or the bottom edge in half-height mode).
	const top = MARKER_BAND_PX * (1 - k);
	ctx.drawImage(
		layer,
		sx,
		0,
		sw,
		layer.height,
		dx,
		anchorY + (top - anchorY) * scale,
		dw,
		layer.height * k * scale
	);
}

/** Paint ONE band's mirrored bars at `gain` (1 for a cached layer). Tri-band
 * keeps the band's colour, per-band normalization and height style; the mono
 * design paints every band in the mono colour against the loudest band's
 * norm, so the three layers' union is the per-point max the combined mono
 * image draws. Columns [x0, x1) of the `w`-wide layer are painted at x - x0. */
/** RGB design (issue #10253): low -> red, mid -> green, high -> blue, each
 * channel the band's share of the column's energy. One band alone is that
 * pure primary; three equal bands are neutral grey. null = silent column. */
export const RGB_MIX_GAIN = 1.5;
const _RGB_STEP = 17; // 16 levels per channel, so a build fills few distinct colours
export function rgbColumnColor(b: { low: number; mid: number; high: number }): string | null {
	const sum = b.low + b.mid + b.high;
	if (!(sum > 0)) return null;
	const ch = (v: number): string => {
		const c = Math.min(255, Math.round((255 * RGB_MIX_GAIN * v) / sum / _RGB_STEP) * _RGB_STEP);
		return c.toString(16).padStart(2, '0');
	};
	return `#${ch(b.low)}${ch(b.mid)}${ch(b.high)}`;
}

export function drawBandLayer(
	ctx: CanvasRenderingContext2D,
	waveform: AnlzWaveform,
	w: number,
	h: number,
	palette: WavePalette,
	design: WaveformDesign,
	band: BandName,
	gain: number,
	x0 = 0,
	x1 = w,
	waveHeight: WaveHeight = 'full'
): void {
	// The rgb design mixes the bands per column, so it bakes gains instead.
	if (design === 'rgb') throw new Error('wave band layer: design rgb has no per-band layers');
	const n = waveform.detail.length;
	if (n === 0) return;
	const values = waveform.detail[band];
	const centerY = MARKER_BAND_PX + (h - MARKER_BAND_PX) / 2;
	const halfH = (h - MARKER_BAND_PX) / 2 - 1;
	const fullH = h - MARKER_BAND_PX - 1;
	const half = waveHeight === 'half';
	const norms = normsFor(waveform);
	const mono = design === 'mono';
	const norm = mono ? Math.max(norms.low, norms.mid, norms.high) : norms[band];
	const style = mono ? 1 : band === 'mid' ? MID_BAND_SCALE : band === 'high' ? HIGH_BAND_SCALE : 1;
	const path = new Path2D();
	for (let x = x0; x < x1; x++) {
		const p0 = Math.max(0, Math.floor((x / w) * n));
		const p1 = Math.min(n - 1, Math.max(p0, Math.ceil(((x + 1) / w) * n) - 1));
		const v = Math.min(1, ampOf(bucketMax(values, p0, p1), norm) * bandHeightScale(gain));
		if (v > 0) {
			if (half) upRect(path, x - x0, h, v * fullH * style);
			else mirrorRect(path, x - x0, centerY, v * halfH * style);
		}
	}
	ctx.fillStyle = mono ? palette.mono : palette[band];
	ctx.fill(path);
}
