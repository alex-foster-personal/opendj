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
import type { AnlzBeat, AnlzCue, AnlzData, AnlzPhrase, AnlzWaveform } from '$lib/rb/types';
import { firstBeatAtOrAfter } from './wave-math';

/** Seconds of track visible across one row (window is centered on the
 * fixed playhead). 24s keeps the <=2400-point detail waveform dense. */
export const WAVE_WINDOW_S = 24;

/** Top strip reserved for beat ticks, cue triangles and phrase chevrons. */
const MARKER_BAND_PX = 10;

/** Playhead is pure white in the screenshot; not a themed surface colour. */
const PLAYHEAD_COLOR = '#ffffff';

/** Rendering style only (matches rekordbox's white core): highs are drawn
 * at reduced height so the white band reads as the inner core. The band
 * DATA is untouched. */
const HIGH_BAND_SCALE = 0.6;
const MID_BAND_SCALE = 0.85;

export interface WavePalette {
	/** Row background (--rb-bg). */
	bg: string;
	/** Lows band - orange (--rb-orange). */
	low: string;
	/** Mids band - blue (--rb-wave-mid). */
	mid: string;
	/** Highs band - near-white overlay (--rb-wave-high). */
	high: string;
	/** Beat/bar ticks (--rb-text). */
	tick: string;
	/** Cue/memory triangles - red (--rb-red). */
	cue: string;
	/** Phrase chevrons (--rb-text-dim). */
	phrase: string;
}

const _PALETTE_VARS: Record<keyof WavePalette, string> = {
	bg: '--rb-bg',
	low: '--rb-orange',
	mid: '--rb-wave-mid',
	high: '--rb-wave-high',
	tick: '--rb-text',
	cue: '--rb-red',
	phrase: '--rb-text-dim'
};

/** Resolve the palette from the .perf-root CSS vars. Fail-fast: a missing
 * var means the element is outside .perf-root - that is a wiring bug. */
export function readPalette(el: HTMLElement): WavePalette {
	const styles = getComputedStyle(el);
	const out = {} as WavePalette;
	for (const key of Object.keys(_PALETTE_VARS) as (keyof WavePalette)[]) {
		const value = styles.getPropertyValue(_PALETTE_VARS[key]).trim();
		if (value === '') {
			throw new Error(
				`wavestack palette: CSS var ${_PALETTE_VARS[key]} empty - element not under .perf-root?`
			);
		}
		out[key] = value;
	}
	return out;
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
}

/** Paint one full row frame. ctx must already be DPR-scaled so all
 * coordinates here are CSS pixels. */
export function drawWaveRow(ctx: CanvasRenderingContext2D, frame: WaveRowFrame): void {
	const { widthCss: w, heightCss: h, palette } = frame;
	ctx.fillStyle = palette.bg;
	ctx.fillRect(0, 0, w, h);

	const durS = frame.durationMs / 1000;
	const tLeft = frame.positionMs / 1000 - WAVE_WINDOW_S / 2;
	const pxPerS = w / WAVE_WINDOW_S;

	if (frame.anlz !== null && durS > 0) {
		_drawBands(ctx, frame.anlz.waveform, tLeft, pxPerS, durS, w, h, palette);
		_drawBeatTicks(ctx, frame.anlz.beatgrid.beats, tLeft, pxPerS, w, palette);
		_drawPhrases(ctx, frame.anlz.phrases, tLeft, pxPerS, w, palette);
		_drawCues(ctx, frame.anlz.cues, tLeft, pxPerS, w, palette);
	}
	_drawPlayhead(ctx, w, h);
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

function _drawBands(
	ctx: CanvasRenderingContext2D,
	waveform: AnlzWaveform,
	tLeft: number,
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

	const lowPath = new Path2D();
	const midPath = new Path2D();
	const highPath = new Path2D();

	for (let x = 0; x < w; x++) {
		const t0 = tLeft + x / pxPerS;
		const t1 = t0 + 1 / pxPerS;
		if (t1 <= 0 || t0 >= durS) continue;
		const p0 = Math.max(0, Math.floor((t0 / durS) * n));
		const p1 = Math.min(n - 1, Math.max(p0, Math.ceil((t1 / durS) * n) - 1));
		if (mono) {
			// Heights only (PWAV/PWV3): the contract does not pin which band
			// array carries them, so take the per-point max across all three.
			const v = Math.max(
				_bucketMax(bands.low, p0, p1),
				_bucketMax(bands.mid, p0, p1),
				_bucketMax(bands.high, p0, p1)
			);
			if (v > 0) _mirrorRect(lowPath, x, centerY, v * halfH);
			continue;
		}
		const lo = _bucketMax(bands.low, p0, p1);
		const mi = _bucketMax(bands.mid, p0, p1);
		const hi = _bucketMax(bands.high, p0, p1);
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

function _drawBeatTicks(
	ctx: CanvasRenderingContext2D,
	beats: AnlzBeat[],
	tLeft: number,
	pxPerS: number,
	w: number,
	palette: WavePalette
): void {
	if (beats.length === 0) return;
	const tRight = tLeft + w / pxPerS;
	ctx.fillStyle = palette.tick;
	for (let i = firstBeatAtOrAfter(beats, tLeft); i < beats.length; i++) {
		const beat = beats[i];
		if (beat.t > tRight) break;
		const x = Math.round((beat.t - tLeft) * pxPerS);
		if (beat.n === 1) {
			// Bar tick: taller + brighter (SCREENSHOT-SPEC 2, COMPONENT-MAP 1.2).
			ctx.globalAlpha = 1;
			ctx.fillRect(x, 0, 2, 8);
		} else {
			ctx.globalAlpha = 0.55;
			ctx.fillRect(x, 0, 1, 4);
		}
	}
	ctx.globalAlpha = 1;
}

function _drawCues(
	ctx: CanvasRenderingContext2D,
	cues: AnlzCue[],
	tLeft: number,
	pxPerS: number,
	w: number,
	palette: WavePalette
): void {
	ctx.fillStyle = palette.cue;
	for (const cue of cues) {
		const x = (cue.in_ms / 1000 - tLeft) * pxPerS;
		if (x < -4 || x > w + 4) continue;
		// Small red down-pointing triangle at the top edge.
		ctx.beginPath();
		ctx.moveTo(x - 4, 0);
		ctx.lineTo(x + 4, 0);
		ctx.lineTo(x, 7);
		ctx.closePath();
		ctx.fill();
	}
}

function _drawPhrases(
	ctx: CanvasRenderingContext2D,
	phrases: AnlzPhrase[],
	tLeft: number,
	pxPerS: number,
	w: number,
	palette: WavePalette
): void {
	if (phrases.length === 0) return;
	ctx.strokeStyle = palette.phrase;
	ctx.lineWidth = 1.5;
	for (const phrase of phrases) {
		const x = (phrase.start_s - tLeft) * pxPerS;
		if (x < -6 || x > w + 6) continue;
		// '>' chevron marking the phrase boundary (SCREENSHOT-SPEC 2).
		ctx.beginPath();
		ctx.moveTo(x, 1.5);
		ctx.lineTo(x + 4, 4.5);
		ctx.lineTo(x, 7.5);
		ctx.stroke();
	}
}

function _drawPlayhead(ctx: CanvasRenderingContext2D, w: number, h: number): void {
	const centerX = Math.round(w / 2);
	ctx.fillStyle = PLAYHEAD_COLOR;
	ctx.globalAlpha = 0.18;
	ctx.fillRect(centerX - 2, 0, 5, h); // soft glow
	ctx.globalAlpha = 1;
	ctx.fillRect(centerX, 0, 1, h);
}
