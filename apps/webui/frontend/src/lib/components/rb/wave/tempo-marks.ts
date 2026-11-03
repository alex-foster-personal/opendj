/**
 * Tempo curve (issue #8339): how both waveform surfaces draw a track's
 * tempo-stability answer from `GET /api/v1/tracks/{id}/tempo-stability`.
 *
 * the maintainer picked this design on Tue 3 Nov 2026 from scored real examples
 * (artifact "Tempo Lane Palettes"): a curve drawn like a transition
 * automation curve, in a band a third of the waveform tall just below the
 * vocal bar. It sits flat on a dotted nominal line, steps for a jump, slopes
 * for a ramp, and its height is percent off nominal (full height at 10
 * percent by default, or the track's own biggest change). A dark scrim and a
 * dark halo keep it readable over the bars and the beat grid. Red is slower,
 * green faster, and a rabbit (faster) or turtle (slower) at the start of each
 * change episode carries direction without color, so red-green color vision
 * deficiency still reads it. Unstable stretches are dashed gray. A steady
 * track paints nothing at all.
 *
 * The daemon decides every segment (#7879 veto); this module only groups
 * back-to-back moving segments into episodes for the one icon each.
 *
 * Icons: Lucide "rabbit" and "turtle", lucide-static 0.460.0, ISC license
 * (https://lucide.dev/license), inlined as their SVG path data.
 */

/** One `/tempo-stability` segment, as the daemon decided it. */
export interface TempoLaneSegment {
	start_s: number;
	end_s: number;
	bpm_start: number;
	bpm_end: number;
	kind: 'steady' | 'ramp' | 'jump' | 'unstable';
	/** 0..1, as served. */
	severity: number;
	direction: 'faster' | 'slower' | 'none';
}

/** The slice of a `/tempo-stability` answer the curve reads. */
export interface TempoCurveData {
	nominal_bpm: number;
	largest_change_pct: number;
	segments: readonly TempoLaneSegment[];
}

/** Colors for the tempo curve on one surface. */
export interface TempoLaneColors {
	slower: string;
	faster: string;
	unstable: string;
}

export const TEMPO_LANE_COLORS = {
	dark: { slower: '#e0443e', faster: '#2fbf4a', unstable: '#a3a29b' },
	light: { slower: '#c43a35', faster: '#1f8f3a', unstable: '#6f6e68' }
} as const satisfies Record<'dark' | 'light', TempoLaneColors>;

/** `tempo_lane_scale`: what full curve height means. */
export type TempoLaneScale = 'fixed_10' | 'relative';
export const TEMPO_CURVE_FIXED_PCT = 10;
/** Relative scale never goes below this, so tracker wobble is not blown up. */
export const TEMPO_CURVE_FLOOR_PCT = 2;

/** Percent off nominal that fills the band, for a scale choice. */
export function tempoCurveFullPct(scale: TempoLaneScale, largestChangePct: number): number {
	if (scale === 'fixed_10') return TEMPO_CURVE_FIXED_PCT;
	else if (scale === 'relative') return Math.max(TEMPO_CURVE_FLOOR_PCT, largestChangePct);
	const _exhaustive: never = scale;
	throw new Error(`Unhandled tempo_lane_scale: ${String(_exhaustive)}`);
}

/** Back-to-back moving segments, one change episode each. */
export interface TempoEpisode {
	start_s: number;
	segments: TempoLaneSegment[];
	/** Direction of the first segment that has one; 'none' for an all-unstable run. */
	direction: 'faster' | 'slower' | 'none';
}

const _ADJACENT_S = 0.05;

export function tempoEpisodes(segments: readonly TempoLaneSegment[]): TempoEpisode[] {
	const out: TempoEpisode[] = [];
	let lastEnd = -Infinity;
	for (const segment of segments) {
		if (segment.kind === 'steady') continue;
		const open = out[out.length - 1];
		if (open !== undefined && Math.abs(segment.start_s - lastEnd) < _ADJACENT_S) open.segments.push(segment);
		else out.push({ start_s: segment.start_s, segments: [segment], direction: 'none' });
		lastEnd = segment.end_s;
	}
	for (const episode of out) {
		episode.direction = episode.segments.find((s) => s.kind !== 'unstable')?.direction ?? 'none';
	}
	return out;
}

const RABBIT = [
	'M13 16a3 3 0 0 1 2.24 5',
	'M18 12h.01',
	'M18 21h-8a4 4 0 0 1-4-4 7 7 0 0 1 7-7h.2L9.6 6.4a1 1 0 1 1 2.8-2.8L15.8 7h.2c3.3 0 6 2.7 6 6v1a2 2 0 0 1-2 2h-1a3 3 0 0 0-3 3',
	'M20 8.54V4a2 2 0 1 0-4 0v3',
	'M7.612 12.524a3 3 0 1 0-1.6 4.3'
] as const;
const TURTLE = [
	'm12 10 2 4v3a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1v-3a8 8 0 1 0-16 0v3a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1v-3l2-4h4Z',
	'M4.82 7.9 8 10',
	'M15.18 7.9 12 10',
	'M16.93 10H20a2 2 0 0 1 0 4H2'
] as const;
let _iconPaths: { rabbit: Path2D[]; turtle: Path2D[] } | null = null;
function _icons(): { rabbit: Path2D[]; turtle: Path2D[] } {
	_iconPaths ??= { rabbit: RABBIT.map((d) => new Path2D(d)), turtle: TURTLE.map((d) => new Path2D(d)) };
	return _iconPaths;
}

/** Geometry of one surface's curve band, in that canvas's own units. */
export interface TempoCurveBand {
	/** Top of the band (just below the vocal bar). */
	top: number;
	/** Band height: a third of the waveform. */
	height: number;
	/** Curve stroke width. */
	lineWidth: number;
	/** Rabbit / turtle size; 0 draws no icons. */
	iconPx: number;
	/** How much wider than tall one canvas unit displays (a stretched strip
	 * backing store); icons are drawn this much narrower so they stay round. */
	xStretch?: number;
}

const SCRIM = 'rgba(10, 10, 10, 0.42)';
const HALO = 'rgba(8, 8, 8, 0.9)';
const BASELINE = 'rgba(163, 162, 155, 0.5)';
const ICON_DISC = 'rgba(16, 16, 16, 0.78)';
const TINT_ALPHA = 0.28;
const UNSTABLE_DASH: readonly number[] = [3, 3];

function _withAlpha(hex: string, alpha: number): string {
	const n = Number.parseInt(hex.slice(1), 16);
	return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function _icon(ctx: CanvasRenderingContext2D, name: 'rabbit' | 'turtle', x: number, top: number, size: number, color: string, xStretch: number): void {
	const width = size / xStretch;
	ctx.save();
	ctx.fillStyle = ICON_DISC;
	ctx.beginPath();
	ctx.ellipse(x + width / 2, top + size / 2, width * 0.62, size * 0.62, 0, 0, Math.PI * 2);
	ctx.fill();
	ctx.translate(x, top);
	ctx.scale(width / 24, size / 24);
	ctx.strokeStyle = color;
	ctx.lineWidth = 2.3;
	ctx.lineCap = 'round';
	ctx.lineJoin = 'round';
	for (const path of _icons()[name]) ctx.stroke(path);
	ctx.restore();
}

/**
 * Draw one surface's tempo curve. `tLeft` / `pxPerS` map track seconds to x
 * (a strip passes 0 and width / duration). A steady answer (no segments)
 * draws nothing, not even the scrim.
 */
export function drawTempoCurve(
	ctx: CanvasRenderingContext2D,
	data: TempoCurveData,
	tLeft: number,
	pxPerS: number,
	w: number,
	band: TempoCurveBand,
	colors: TempoLaneColors,
	scale: TempoLaneScale
): void {
	const episodes = tempoEpisodes(data.segments);
	if (episodes.length === 0) return;
	const x = (t: number): number => (t - tLeft) * pxPerS;
	const half = band.height / 2;
	const base = band.top + half;
	const fullPct = tempoCurveFullPct(scale, data.largest_change_pct);
	const y = (bpm: number): number => {
		const pct = ((bpm - data.nominal_bpm) / data.nominal_bpm) * 100;
		return base - Math.max(-1, Math.min(1, pct / fullPct)) * half;
	};
	ctx.save();
	ctx.fillStyle = SCRIM;
	ctx.fillRect(0, band.top, w, band.height);
	ctx.strokeStyle = BASELINE;
	ctx.lineWidth = Math.max(0.5, band.lineWidth * 0.4);
	ctx.setLineDash([2, 3]);
	ctx.beginPath();
	ctx.moveTo(0, base);
	ctx.lineTo(w, base);
	ctx.stroke();
	ctx.lineJoin = 'round';
	ctx.lineCap = 'round';
	type Piece = { color: string; dashed: boolean; tint: boolean; pts: [number, number][] };
	const pieces: Piece[] = [];
	for (const episode of episodes) {
		let px = x(episode.start_s);
		let py = base;
		for (const segment of episode.segments) {
			const unstable = segment.kind === 'unstable';
			// Color by position, not by motion: red while the tempo sits below nominal,
			// green above (midpoint of the piece). The slope already shows which way it
			// moves, so a rising ramp still below nominal reads red, not green.
			const midBpm = (segment.bpm_start + segment.bpm_end) / 2;
			const color = unstable ? colors.unstable : midBpm < data.nominal_bpm ? colors.slower : colors.faster;
			const pts: [number, number][] = [[px, py], [x(segment.start_s), y(segment.bpm_start)], [x(segment.end_s), y(segment.bpm_end)]];
			pieces.push({ color, dashed: unstable, tint: !unstable, pts });
			px = x(segment.end_s);
			py = y(segment.bpm_end);
		}
		pieces.push({ color: BASELINE, dashed: false, tint: false, pts: [[px, py], [px, base]] });
	}
	for (const piece of pieces) {
		if (!piece.tint) continue;
		const [, a, b] = piece.pts;
		ctx.fillStyle = _withAlpha(piece.color, TINT_ALPHA);
		ctx.beginPath();
		ctx.moveTo(a[0], base);
		ctx.lineTo(a[0], a[1]);
		ctx.lineTo(b[0], b[1]);
		ctx.lineTo(b[0], base);
		ctx.closePath();
		ctx.fill();
	}
	for (const pass of ['halo', 'ink'] as const) {
		for (const piece of pieces) {
			ctx.setLineDash(piece.dashed ? UNSTABLE_DASH : []);
			ctx.strokeStyle = pass === 'halo' ? HALO : piece.color;
			ctx.lineWidth = pass === 'halo' ? band.lineWidth + 2.5 : band.lineWidth;
			ctx.beginPath();
			piece.pts.forEach(([px, py], i) => (i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py)));
			ctx.stroke();
		}
	}
	ctx.setLineDash([]);
	if (band.iconPx > 0) {
		const stretch = band.xStretch ?? 1;
		const iconW = band.iconPx / stretch;
		let lastX = -Infinity;
		for (const episode of episodes) {
			if (episode.direction === 'none') continue;
			const sx = x(episode.start_s);
			if (sx < -iconW || sx > w || sx - lastX < iconW * 1.3) continue;
			const ix = Math.max(0, Math.min(w - iconW, sx - iconW - 2 / stretch));
			const color = episode.direction === 'slower' ? colors.slower : colors.faster;
			_icon(ctx, episode.direction === 'slower' ? 'turtle' : 'rabbit', ix, band.top, band.iconPx, color, stretch);
			lastX = sx;
		}
	}
	ctx.restore();
}
