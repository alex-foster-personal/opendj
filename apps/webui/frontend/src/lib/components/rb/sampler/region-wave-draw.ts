/**
 * Draw a slot's RegionPeaks the way the deck waves are drawn (spec 7.4 "Tab waveform is the
 * app's own waveform"): the active 3-band palette, low behind, mid at MID_BAND_SCALE, high at
 * HIGH_BAND_SCALE, mirrored about the centre in 1 px columns, across exactly the region the
 * server cut on the track's own beatgrid. No loop overlay, no BPM input: the columns are the
 * region's peaks and nothing else.
 */

import { HIGH_BAND_SCALE, MID_BAND_SCALE, bucketMax } from '$lib/components/rb/wave/wave-amp';
import type { RegionPeaks } from '$lib/player/sampler/sampler-types';
import type { WaveBandColors } from '$lib/rb/wave-palette';

/** Fraction of the half-height the loudest column reaches on a tab (mock `drawTabWave`, `H * .46`). */
const TAB_WAVE_FILL = 0.92;
/** ...and on the edit modal's CUE-only deck (mock `drawCueDeckWave`, `H * .44`). */
export const CUE_DECK_WAVE_FILL = 0.88;

/** Per-column band heights (0..1 of the half-height) that tile all of `peaks` across `width` columns. */
export function regionColumnHeights(
	peaks: RegionPeaks,
	width: number,
	fill = TAB_WAVE_FILL
): { low: number[]; mid: number[]; high: number[] } {
	const n = peaks.low.length;
	if (n === 0 || peaks.mid.length !== n || peaks.high.length !== n) {
		throw new Error(
			`region wave: bands must be the same non-zero length (low ${n}, mid ${peaks.mid.length}, high ${peaks.high.length})`
		);
	}
	const out = { low: [] as number[], mid: [] as number[], high: [] as number[] };
	for (let x = 0; x < width; x++) {
		const p0 = Math.floor((x / width) * n);
		const p1 = Math.max(p0, Math.min(n - 1, Math.floor(((x + 1) / width) * n) - 1));
		out.low.push(Math.min(1, bucketMax(peaks.low, p0, p1)) * fill);
		out.mid.push(Math.min(1, bucketMax(peaks.mid, p0, p1)) * fill * MID_BAND_SCALE);
		out.high.push(Math.min(1, bucketMax(peaks.high, p0, p1)) * fill * HIGH_BAND_SCALE);
	}
	return out;
}

function fitCanvas(cv: HTMLCanvasElement): CanvasRenderingContext2D {
	const dpr = window.devicePixelRatio || 1;
	const w = Math.max(1, Math.round(cv.clientWidth * dpr));
	const h = Math.max(1, Math.round(cv.clientHeight * dpr));
	if (cv.width !== w) cv.width = w;
	if (cv.height !== h) cv.height = h;
	const g = cv.getContext('2d');
	if (g === null) throw new Error('region wave: no 2d context');
	return g;
}

/** Paint `peaks` into `cv` at its CSS size and the device pixel ratio, loudest at `fill`. */
export function drawRegionPeaks(
	cv: HTMLCanvasElement,
	peaks: RegionPeaks,
	colors: WaveBandColors,
	fill = TAB_WAVE_FILL
): void {
	const g = fitCanvas(cv);
	const cols = regionColumnHeights(peaks, cv.width, fill);
	const half = cv.height / 2;
	g.clearRect(0, 0, cv.width, cv.height);
	for (const band of ['low', 'mid', 'high'] as const) {
		const path = new Path2D();
		cols[band].forEach((v, x) => path.rect(x, half - v * half, 1, v * half * 2));
		g.fillStyle = colors[band];
		g.fill(path);
	}
}

/** One beatgrid line of the CUE-only deck: column, width and opacity (downbeats brighter). */
export interface BeatTick {
	x: number;
	w: number;
	alpha: number;
	down: boolean;
}

/** Beat lines across `beats` beats in `width` columns; `downbeatOffset` moves beat 1 (mock `drawCueDeckWave`). */
export function beatTicks(
	beats: number,
	width: number,
	downbeatOffset = 0,
	beatsPerBar = 4
): BeatTick[] {
	if (beats <= 0) return [];
	const off = ((downbeatOffset % beatsPerBar) + beatsPerBar) % beatsPerBar;
	return Array.from({ length: beats + 1 }, (_, b) => {
		const down = (((b - off) % beatsPerBar) + beatsPerBar) % beatsPerBar === 0;
		const x = Math.min(width - 2, Math.round((b / beats) * width));
		return { x, w: down ? 2 : 1, alpha: down ? 0.85 : 0.3, down };
	});
}

/** Paint the region's beatgrid over a wave already drawn in `cv`, in device pixels. */
export function drawRegionBeats(
	cv: HTMLCanvasElement,
	beats: number,
	downbeatOffset: number
): void {
	const g = fitCanvas(cv);
	const dpr = window.devicePixelRatio || 1;
	g.fillStyle = '#ffffff';
	for (const t of beatTicks(beats, cv.width, downbeatOffset)) {
		g.globalAlpha = t.alpha;
		g.fillRect(t.x, 0, Math.max(1, Math.round(t.w * dpr)), cv.height);
	}
	g.globalAlpha = 1;
}
