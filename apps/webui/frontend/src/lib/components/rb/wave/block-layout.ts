/**
 * Change-snapped geometry for the 'blocks' (Gothic) waveform design.
 *
 * A block is the bucket MAX of the detail points under it, so on a fixed
 * BLOCK_PITCH_PX grid a kick that starts mid-block lights the WHOLE block,
 * and the visible edge of the hit lands up to two pixels before the real
 * attack. Across a row the beat edges wobble against the grid lines.
 *
 * Here the block boundaries bend instead: every moderate-or-larger change
 * (an energy jump in the detail data, or a beat of a trusted grid) becomes
 * an ANCHOR, and the run between two anchors is split into whole blocks
 * whose pitch varies within BLOCK_PITCH_MIN_PX..BLOCK_PITCH_MAX_PX. So the
 * attack sits exactly on a block's left edge and the block before it holds
 * none of it. The gap between bars stays 1px; only bar width varies.
 *
 * Cost: change points are found once per waveform (memoized, O(points));
 * edges are laid out once per cached band image (O(blocks)). Nothing here
 * runs per animation frame (render.ts _drawCachedBands).
 */
import type { AnlzBeat, AnlzWaveform } from '$lib/rb/anlz-types';
import { visibleBeatLines } from './wave-math';

export const BLOCK_SNAP_CFG = {
	/** Nominal block pitch; equals render.ts BLOCK_PITCH_PX. */
	PITCH_PX: 3,
	/** Anchors closer than this merge (the higher priority one wins; between two
	 * changes, the earlier). A block is a 1 px gap plus at least a 1 px bar, so
	 * two edges under 2 px apart cannot both start one. */
	MIN_ANCHOR_GAP_PX: 2,
	/** A beat this close to an energy change yields to the change: the data
	 * edge is what the bars show, the grid line is painted on top anyway. */
	BEAT_YIELD_PX: 3,
	/** "Moderate" change: the display-scaled level (v^AMP_GAMMA of the
	 * painter's own max band level, ChangeLevelNorm) must rise by this much
	 * above the max of the LOOKBACK before it. Display scale, so it matches
	 * what bar heights show. 0.15 of the half height, on the two real 24 s
	 * fixture windows: 5 changes on the clip window (all within 30 ms of a
	 * beat) and 36 on the dense-mix window, where 0.1 fires 9 and 74 times
	 * and 0.2 drops to 9 on the dense mix. */
	RISE: 0.15,
	LOOKBACK_S: 0.04,
	/** One change per hit: the next can start this long after the last. */
	REFRACTORY_S: 0.08,
	/** Same display curve as render.ts AMP_GAMMA. */
	AMP_GAMMA: 0.45
} as const;

/** Narrowest and widest pitch the layout can produce (see snappedBlockEdges). */
export const BLOCK_PITCH_MIN_PX = 2;
export const BLOCK_PITCH_MAX_PX = 4;

/** The level the 'max' block painter shows for one point: the louder of the
 * three bands over the row's own divisor (render.ts blockHeights: true peaks
 * for rekordbox rows, p99 norms clamped at 1 for self-decoded ones). */
export interface ChangeLevelNorm {
	/** max(low, mid, high divisors), exactly the painter's monoNorm. */
	divisor: number;
	/** true for the 'legacy-p99' curve, which clamps at full height. */
	clampAtOne: boolean;
}

const _changeCache = new WeakMap<AnlzWaveform, Map<string, Int32Array>>();

/** Detail-point indices where the painted level steps up by a moderate amount
 * or more. Pure in the band data; memoized per waveform object and norm, so it
 * still runs once per track load. */
export function changePointsFor(waveform: AnlzWaveform, durationS: number, norm: ChangeLevelNorm): Int32Array {
	let byNorm = _changeCache.get(waveform);
	if (byNorm === undefined) {
		byNorm = new Map();
		_changeCache.set(waveform, byNorm);
	}
	const key = `${norm.divisor}|${norm.clampAtOne ? 1 : 0}|${durationS}`;
	const cached = byNorm.get(key);
	if (cached !== undefined) return cached;
	const found = detectChangePoints(waveform.detail, durationS, norm);
	byNorm.set(key, found);
	return found;
}

/** See changePointsFor. Exported for tests. Throws on a duration that is not
 * finite and positive, or a divisor that is not: render.ts only paints rows
 * with both, so anything else is an upstream defect, not a track without
 * changes. */
export function detectChangePoints(bands: AnlzWaveform['detail'], durationS: number, norm: ChangeLevelNorm): Int32Array {
	if (!Number.isFinite(durationS) || durationS <= 0) {
		throw new RangeError(`detectChangePoints: duration must be finite and positive, got ${durationS}`);
	}
	if (!Number.isFinite(norm.divisor) || norm.divisor <= 0) {
		throw new RangeError(`detectChangePoints: divisor must be finite and positive, got ${norm.divisor}`);
	}
	const n = bands.low.length;
	if (n < 2) return new Int32Array(0);
	const cfg = BLOCK_SNAP_CFG;
	const ptsPerS = n / durationS;
	const look = Math.max(1, Math.round(cfg.LOOKBACK_S * ptsPerS));
	// Ceil, not round: rounding down at some rates would let two changes land
	// closer than REFRACTORY_S (126 points/s rounds 10.08 to 10, 79.4 ms).
	const refractory = Math.max(1, Math.ceil(cfg.REFRACTORY_S * ptsPerS));
	const level = new Float32Array(n);
	for (let i = 0; i < n; i++) {
		let v = Math.max(bands.low[i], bands.mid[i], bands.high[i]) / norm.divisor;
		if (norm.clampAtOne && v > 1) v = 1;
		level[i] = v > 0 ? Math.pow(v, cfg.AMP_GAMMA) : 0;
	}
	const out: number[] = [];
	let last = -refractory;
	let inRise = false;
	// Max of the `look` points before i, as a monotonic deque of indices
	// (levels decreasing front to back): each point enters and leaves once,
	// so the pass is O(points) whatever the lookback.
	const dq = new Int32Array(n);
	let head = 0;
	let tail = 0;
	for (let i = 1; i < n; i++) {
		while (tail > head && level[dq[tail - 1]] <= level[i - 1]) tail--;
		dq[tail++] = i - 1;
		while (dq[head] < i - look) head++;
		const prior = level[dq[head]];
		const rising = level[i] - prior >= cfg.RISE;
		// First point of a rise only: a rise spanning several points is ONE
		// change, anchored where it starts.
		if (rising && !inRise && i - last >= refractory) {
			out.push(i);
			last = i;
		}
		inRise = rising;
	}
	return Int32Array.from(out);
}

/** Block left edges (plus the closing right edge, which may pass `widthPx`
 * by up to a pitch, as on the fixed grid) across `widthPx`, with a left edge
 * on every anchor that survives merging. `changeX` are energy
 * changes, FLOORED by the caller so the block before a change never reaches
 * its detail point; `beatX` beats, ROUNDED like the painted grid lines
 * (wave-math.ts visibleBeatLines), since a beat has no detail point. Every
 * block's pitch is BLOCK_PITCH_MIN_PX..BLOCK_PITCH_MAX_PX (anchors sit at
 * least MIN_ANCHOR_GAP_PX from each other). Anchors at the row's ends are
 * kept: one at pixel 1, too close to 0 for a block before it, starts the
 * first block there and leaves pixel 0 unpainted; one in the last pixels
 * starts a fixed-pitch block running past `widthPx`, like the tail. */
export function snappedBlockEdges(
	widthPx: number,
	changeX: ArrayLike<number>,
	beatX: ArrayLike<number>
): Int32Array {
	const cfg = BLOCK_SNAP_CFG;
	const w = Math.max(1, Math.floor(widthPx));
	const anchors = _mergeAnchors(w, changeX, beatX);
	const edges: number[] = [];
	let a = anchors.length > 0 && anchors[0] < cfg.MIN_ANCHOR_GAP_PX ? anchors[0] : 0;
	for (const b of anchors) {
		if (b <= a) continue;
		const span = b - a;
		const k = Math.max(1, Math.round(span / cfg.PITCH_PX));
		for (let i = 0; i < k; i++) edges.push(a + Math.round((i * span) / k));
		a = b;
	}
	// After the last anchor nothing needs to land on w, so the tail keeps the
	// fixed pitch, last block running past w exactly like uniformBlockEdges:
	// with no anchors at all this IS the old grid, for any width.
	for (; a < w; a += cfg.PITCH_PX) edges.push(a);
	edges.push(a);
	return Int32Array.from(edges);
}

function _mergeAnchors(w: number, changeX: ArrayLike<number>, beatX: ArrayLike<number>): number[] {
	const cfg = BLOCK_SNAP_CFG;
	// Every pixel of the row can carry an anchor (see snappedBlockEdges).
	const lo = 0;
	const hi = w - 1;
	// Changes first: they are what the bars show. Both inputs are sorted.
	const changes: number[] = [];
	for (let i = 0; i < changeX.length; i++) {
		const x = changeX[i];
		if (x < lo || x > hi) continue;
		if (changes.length > 0 && x - changes[changes.length - 1] < cfg.MIN_ANCHOR_GAP_PX) continue;
		changes.push(x);
	}
	const out: number[] = [];
	let c = 0;
	let prev = -Infinity;
	const take = (x: number) => {
		if (x - prev < cfg.MIN_ANCHOR_GAP_PX) return;
		out.push(x);
		prev = x;
	};
	for (let i = 0; i < beatX.length; i++) {
		const x = beatX[i];
		while (c < changes.length && changes[c] <= x - cfg.BEAT_YIELD_PX) take(changes[c++]);
		if (x < lo || x > hi) continue;
		// Every earlier change is now >= BEAT_YIELD_PX behind; yield to one
		// less than BEAT_YIELD_PX ahead.
		if (c < changes.length && changes[c] < x + cfg.BEAT_YIELD_PX) continue;
		take(x);
	}
	while (c < changes.length) take(changes[c++]);
	return out;
}

/** Beat anchors in px for a band image laid out at `pxPerS`: the lines the
 * grid painter itself selects across the whole track (visibleBeatLines with
 * origin 0), so a beat the grid thins away under BEAT_LINE_MIN_GAP_PX
 * (WAVEZOOM-02) bends no block, and each kept line's edge sits on its own
 * rounded pixel. The stride is the one the track's closest beats need, which
 * is every window's stride on a steady tempo. */
export function beatAnchorsPx(beats: readonly AnlzBeat[] | null, pxPerS: number, barPhaseUntrusted = false): Int32Array {
	if (beats === null) return new Int32Array(0);
	const lines = visibleBeatLines(beats, 0, pxPerS, Infinity, 0, barPhaseUntrusted, 0);
	return Int32Array.from(lines, (l) => l.x);
}

/** Change anchors in px for a surface where detail point i sits at i*w/n. */
export function changeAnchorsPx(points: Int32Array, n: number, widthPx: number): Int32Array {
	const out = new Int32Array(points.length);
	for (let i = 0; i < points.length; i++) out[i] = Math.floor((points[i] * widthPx) / n);
	return out;
}

/** The snapped edges for one deck row `widthPx` wide at `pxPerS`: what
 * render.ts asks for, in one call so the library chunk names one export. */
export function rowBlockEdges(
	waveform: AnlzWaveform,
	durationS: number,
	norm: ChangeLevelNorm,
	widthPx: number,
	beats: readonly AnlzBeat[] | null,
	pxPerS: number,
	barPhaseUntrusted = false
): Int32Array {
	const changes = changeAnchorsPx(changePointsFor(waveform, durationS, norm), waveform.detail.low.length, widthPx);
	return snappedBlockEdges(widthPx, changes, beatAnchorsPx(beats, pxPerS, barPhaseUntrusted));
}

const _beatsIds = new WeakMap<readonly AnlzBeat[], number>();
let _nextBeatsId = 1;
/** A number per beats array (the grid is replaced, never edited in place,
 * PERF-GRID-01) plus its length: render.ts keys the band image on it so a grid
 * edit rebuilds the snapped edges. Here, not there, to keep it off "/". */
export function beatsVersion(beats: readonly AnlzBeat[] | null, barPhaseUntrusted = false): string {
	if (beats === null) return 's0';
	let id = _beatsIds.get(beats);
	if (id === undefined) {
		id = _nextBeatsId++;
		_beatsIds.set(beats, id);
	}
	// The bar-phase flag picks which beats the thinned grid keeps.
	return `s${id}.${beats.length}${barPhaseUntrusted ? 'u' : ''}`;
}
