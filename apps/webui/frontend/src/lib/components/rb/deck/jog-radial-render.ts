/**
 * Polar jog-dial waveform painter (DECKUX-02). Maps preview PWAV band heights
 * and PVDI/demucs vocal regions onto the deck wheel face. Presentational only:
 * no api-rb, no engine position - the SVG progress trail carries position.
 */
import {
	JOG_FACE_CLEARANCE,
	JOG_WHEEL_FACE_RADIUS,
	JOG_WHEEL_FACE_STROKE_WIDTH
} from '../wave/wave-math';
import { vocalAlpha, WAVE_UPPER_BAND_ALPHA, type WavePalette } from '../wave/render';

/** Translucent scrim behind the BPM/pitch/range text (viewBox units, center 50,50). */
export const JOG_RADIAL_INNER_RADIUS = 18;

/** Waveform baseline: bands start near the hub and run UNDER the text scrim,
 * so the face reads as one waveform instead of a ring around a black disc. */
export const JOG_RADIAL_WAVE_INNER_RADIUS = 6;

/** Stays inside the face disc; must not enter the phase-mark annulus. */
export const JOG_RADIAL_OUTER_RADIUS =
	JOG_WHEEL_FACE_RADIUS - JOG_WHEEL_FACE_STROKE_WIDTH / 2 - JOG_FACE_CLEARANCE;

/** Copied from wave/render.ts - pinned by jog-radial-waveform.test.mjs. */
export const JOG_RADIAL_AMP_GAMMA = 0.45;
export const JOG_RADIAL_HIGH_BAND_SCALE = 0.6;
export const JOG_RADIAL_MID_BAND_SCALE = 0.85;
export const JOG_RADIAL_NORM_FLOOR = 0.1;

/** Band colours come from the SAME source as the main wavestack rows:
 * readPalette() over the .perf-root --rb-wave-* vars, so the wheel follows the
 * theme, the skin and Settings > Waveform colors exactly like the deck rows.
 * No colour literal lives in this file (jog-radial-waveform.test.mjs pins it). */
export type JogRadialPalette = Pick<WavePalette, 'low' | 'mid' | 'high' | 'mono' | 'vocal'>;

export interface StripVocalRegion {
	start_s: number;
	end_s: number;
	intensity: number;
}

export type StripVocals =
	| { status: 'rekordbox'; regions: readonly StripVocalRegion[] }
	| { status: 'demucs'; regions: readonly StripVocalRegion[] }
	| { status: 'no_vocals'; regions: readonly StripVocalRegion[] }
	| { status: 'not_analyzed' };

export interface JogRadialPreview {
	length: number;
	low: readonly number[];
	mid: readonly number[];
	high: readonly number[];
}

export interface JogRadialFrame {
	widthPx: number;
	heightPx: number;
	palette: JogRadialPalette;
	kind: 'tri' | 'mono';
	preview: JogRadialPreview;
	vocals: StripVocals | null;
	durationSec: number | null;
}

interface BandNorms {
	low: number;
	mid: number;
	high: number;
}

const _imageCache = new Map<string, HTMLCanvasElement>();
let _refSerial = 0;
const _refTags = new WeakMap<object, number>();

function _refTag(ref: object): number {
	let tag = _refTags.get(ref);
	if (tag === undefined) {
		tag = ++_refSerial;
		_refTags.set(ref, tag);
	}
	return tag;
}

/** Test-only cache reset. */
export function resetJogRadialCacheForTest(): void {
	_imageCache.clear();
}

export function polarAngleRad(index: number, count: number): number {
	return -Math.PI / 2 + (2 * Math.PI * index) / count;
}

export function polarRadius(amp01: number, rInner: number, rOuter: number): number {
	const clamped = Math.max(0, Math.min(1, amp01));
	return rInner + clamped * (rOuter - rInner);
}

export function vocalArcAngles(
	region: StripVocalRegion,
	durationSec: number
): { startRad: number; endRad: number } {
	if (!(durationSec > 0)) {
		throw new Error('vocalArcAngles: durationSec must be positive');
	}
	return {
		startRad: polarAngleRad(0, 1) + (2 * Math.PI * region.start_s) / durationSec,
		endRad: polarAngleRad(0, 1) + (2 * Math.PI * region.end_s) / durationSec
	};
}

/** Cache key for the offscreen polar image. Must never include position. */
export function jogRadialCacheKey(
	frame: JogRadialFrame,
	previewRef: object,
	vocalsRef: object | null,
	dpr: number
): string {
	return [
		String(dpr),
		frame.widthPx,
		frame.heightPx,
		frame.palette.low,
		frame.palette.mid,
		frame.palette.high,
		frame.palette.mono,
		frame.palette.vocal,
		frame.kind,
		String(frame.preview.length),
		String(frame.durationSec ?? ''),
		String(_refTag(previewRef)),
		vocalsRef === null ? 'null' : String(_refTag(vocalsRef)),
		frame.preview.low[0],
		frame.preview.low[frame.preview.length - 1],
		frame.preview.high[0],
		frame.preview.high[frame.preview.length - 1]
	].join('|');
}

function _p99(values: readonly number[]): number {
	const nonZero = values.filter((v) => v > 0);
	if (nonZero.length === 0) return 1;
	const sorted = nonZero.slice().sort((a, b) => a - b);
	const p99 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.99))];
	return Math.min(1, Math.max(JOG_RADIAL_NORM_FLOOR, p99));
}

function _normsFor(preview: JogRadialPreview): BandNorms {
	return {
		low: _p99(preview.low),
		mid: _p99(preview.mid),
		high: _p99(preview.high)
	};
}

function _amp(v: number, norm: number): number {
	if (v <= 0) return 0;
	return Math.pow(Math.min(1, v / norm), JOG_RADIAL_AMP_GAMMA);
}

function _vbScale(widthPx: number): number {
	return widthPx / 100;
}

function _fillWedge(
	ctx: CanvasRenderingContext2D,
	cx: number,
	cy: number,
	scale: number,
	rInner: number,
	rOuter: number,
	a0: number,
	a1: number,
	color: string
): void {
	if (rOuter <= rInner) return;
	ctx.fillStyle = color;
	ctx.beginPath();
	ctx.arc(cx, cy, rOuter * scale, a0, a1);
	ctx.arc(cx, cy, rInner * scale, a1, a0, true);
	ctx.closePath();
	ctx.fill();
}

function _paintBands(
	ctx: CanvasRenderingContext2D,
	frame: JogRadialFrame,
	cx: number,
	cy: number,
	scale: number,
	palette: JogRadialPalette,
	norms: BandNorms
): void {
	const n = frame.preview.length;
	if (n === 0) return;
	const rInner = JOG_RADIAL_WAVE_INNER_RADIUS;
	const rOuter = JOG_RADIAL_OUTER_RADIUS;
	for (let i = 0; i < n; i++) {
		const a0 = polarAngleRad(i, n);
		const a1 = polarAngleRad(i + 1, n);
		if (frame.kind === 'mono') {
			const v = Math.max(frame.preview.low[i], frame.preview.mid[i], frame.preview.high[i]);
			const amp = _amp(v, Math.max(norms.low, norms.mid, norms.high));
			const r = polarRadius(amp, rInner, rOuter);
			_fillWedge(ctx, cx, cy, scale, rInner, r, a0, a1, palette.mono);
			continue;
		}
		const lo = _amp(frame.preview.low[i], norms.low);
		const mi = _amp(frame.preview.mid[i], norms.mid);
		const hi = _amp(frame.preview.high[i], norms.high);
		const rLo = polarRadius(lo, rInner, rOuter);
		const rMi = polarRadius(mi * JOG_RADIAL_MID_BAND_SCALE, rInner, rOuter);
		const rHi = polarRadius(hi * JOG_RADIAL_HIGH_BAND_SCALE, rInner, rOuter);
		_fillWedge(ctx, cx, cy, scale, rInner, rLo, a0, a1, palette.low);
		// Same layering as the deck rows (render.ts _drawBands): opaque low,
		// mid and high at WAVE_UPPER_BAND_ALPHA so they read through each other.
		ctx.globalAlpha = WAVE_UPPER_BAND_ALPHA;
		_fillWedge(ctx, cx, cy, scale, rInner, rMi, a0, a1, palette.mid);
		_fillWedge(ctx, cx, cy, scale, rInner, rHi, a0, a1, palette.high);
		ctx.globalAlpha = 1;
	}
}

function _paintVocals(
	ctx: CanvasRenderingContext2D,
	frame: JogRadialFrame,
	cx: number,
	cy: number,
	scale: number
): void {
	const vocals = frame.vocals;
	const durationSec = frame.durationSec;
	if (vocals === null || durationSec === null || !(durationSec > 0)) return;
	if (vocals.status !== 'rekordbox' && vocals.status !== 'demucs') return;
	const rInner = (JOG_RADIAL_OUTER_RADIUS - 2) * scale;
	const rOuter = JOG_RADIAL_OUTER_RADIUS * scale;
	ctx.fillStyle = frame.palette.vocal;
	for (const region of vocals.regions) {
		const { startRad, endRad } = vocalArcAngles(region, durationSec);
		if (endRad <= startRad) continue;
		ctx.globalAlpha = vocalAlpha(region.intensity);
		ctx.beginPath();
		ctx.arc(cx, cy, rOuter, startRad, endRad);
		ctx.arc(cx, cy, rInner, endRad, startRad, true);
		ctx.closePath();
		ctx.fill();
	}
	ctx.globalAlpha = 1;
}

/** Paint one polar frame onto `ctx` (clears first). No-ops when preview is empty. */
export function paintJogRadial(ctx: CanvasRenderingContext2D, frame: JogRadialFrame): void {
	const { widthPx, heightPx } = frame;
	ctx.clearRect(0, 0, widthPx, heightPx);
	if (frame.preview.length === 0) return;
	const cx = widthPx / 2;
	const cy = heightPx / 2;
	const scale = _vbScale(widthPx);
	const norms = _normsFor(frame.preview);
	_paintBands(ctx, frame, cx, cy, scale, frame.palette, norms);
	_paintVocals(ctx, frame, cx, cy, scale);
}

/**
 * Blit a cached offscreen polar image. Cache key omits position by design
 * (DECKUX-02 acceptance: paint is not keyed on playback position).
 */
export function blitJogRadial(
	ctx: CanvasRenderingContext2D,
	frame: JogRadialFrame,
	previewRef: object,
	vocalsRef: object | null,
	dpr: number
): void {
	const key = jogRadialCacheKey(frame, previewRef, vocalsRef, dpr);
	let off = _imageCache.get(key);
	if (off === undefined) {
		off = document.createElement('canvas');
		off.width = Math.round(frame.widthPx * dpr);
		off.height = Math.round(frame.heightPx * dpr);
		const offCtx = off.getContext('2d');
		if (offCtx === null) throw new Error('jog-radial-render: offscreen 2d context unavailable');
		offCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
		paintJogRadial(offCtx, frame);
		_imageCache.set(key, off);
	}
	ctx.clearRect(0, 0, frame.widthPx, frame.heightPx);
	ctx.drawImage(off, 0, 0, frame.widthPx, frame.heightPx);
}
