/**
 * WAVE-EQ-5230: the wavestack row's per-band image cache, so a deck's live EQ
 * and filter can rescale each ANLZ band at blit time.
 *
 * One transparent full-track image PER BAND (low, mid, high), keyed once per
 * waveform, zoom, size, design and palette, and held as tiles of `tilePx`
 * columns built only when a visible span first needs them, like the combined
 * image (WAVEZOOM-01: a whole-track canvas at close zoom outgrows what
 * Chromium paints). A gain change then costs scaled blits and three
 * multiplies, never a geometry rebuild. Used only while the engine reports
 * gains; without them render.ts draws its single combined image as before.
 */
import type { AnlzWaveform } from '$lib/rb/anlz-types';
import type { BandGains } from '$lib/rb/wave-eq-gains.svelte';
import type { WaveformDesign } from '$lib/rb/waveform-design';
import { MARKER_BAND_PX, type WavePalette } from './cues';
import {
	ampOf,
	bandHeightScale,
	bucketMax,
	HIGH_BAND_SCALE,
	MID_BAND_SCALE,
	mirrorRect,
	normsFor
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
 * or when `reusable` (render.ts bandImageScaleReusable, PERF-GRID-02) says the
 * live zoom drifted too far from the one it was built at. Tiles are built
 * lazily by blitBandLayers. */
export function cachedBandLayers(
	waveform: AnlzWaveform,
	key: string,
	pxPerS: number,
	durS: number,
	h: number,
	palette: WavePalette,
	design: WaveformDesign,
	reusable: (builtPxPerS: number, livePxPerS: number) => boolean,
	tilePx: number
): BandLayers {
	const hit = _layerCache.get(waveform);
	if (hit !== undefined && hit.key === key && reusable(hit.layers.pxPerS, pxPerS))
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
		h
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
			x0 + tileW
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
	const sLow = bandHeightScale(gains.low);
	const sMid = bandHeightScale(gains.mid);
	const sHigh = bandHeightScale(gains.high);
	const boosted = sLow > 1 || sMid > 1 || sHigh > 1;
	if (boosted) {
		ctx.save();
		ctx.beginPath();
		ctx.rect(0, centerY - halfH, w, halfH * 2);
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
		_blitLayer(ctx, tiles[0], sLow, tsx - x0, tsw, dx, dw, centerY, h);
		ctx.globalAlpha = alpha;
		_blitLayer(ctx, tiles[1], sMid, tsx - x0, tsw, dx, dw, centerY, h);
		_blitLayer(ctx, tiles[2], sHigh, tsx - x0, tsw, dx, dw, centerY, h);
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
	centerY: number,
	h: number
): void {
	if (scale === 0) return;
	// Source-rect form: name only the visible columns of this tile.
	ctx.drawImage(layer, sx, 0, sw, h, dx, centerY - centerY * scale, dw, h * scale);
}

/** Paint ONE band's mirrored bars at `gain` (1 for a cached layer). Tri-band
 * keeps the band's colour, per-band normalization and height style; the mono
 * design paints every band in the mono colour against the loudest band's
 * norm, so the three layers' union is the per-point max the combined mono
 * image draws. Columns [x0, x1) of the `w`-wide layer are painted at x - x0. */
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
	x1 = w
): void {
	const n = waveform.detail.length;
	if (n === 0) return;
	const values = waveform.detail[band];
	const centerY = MARKER_BAND_PX + (h - MARKER_BAND_PX) / 2;
	const halfH = (h - MARKER_BAND_PX) / 2 - 1;
	const norms = normsFor(waveform);
	const mono = design === 'mono';
	const norm = mono ? Math.max(norms.low, norms.mid, norms.high) : norms[band];
	const style = mono ? 1 : band === 'mid' ? MID_BAND_SCALE : band === 'high' ? HIGH_BAND_SCALE : 1;
	const path = new Path2D();
	for (let x = x0; x < x1; x++) {
		const p0 = Math.max(0, Math.floor((x / w) * n));
		const p1 = Math.min(n - 1, Math.max(p0, Math.ceil(((x + 1) / w) * n) - 1));
		const v = Math.min(1, ampOf(bucketMax(values, p0, p1), norm) * bandHeightScale(gain));
		if (v > 0) mirrorRect(path, x - x0, centerY, v * halfH * style);
	}
	ctx.fillStyle = mono ? palette.mono : palette[band];
	ctx.fill(path);
}
