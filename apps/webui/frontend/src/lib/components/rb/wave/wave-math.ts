/**
 * Pure time/beat math for the wavestack rows (build unit: wavestack).
 * No runes, no DOM - unit-testable helpers only.
 */
import type { AnlzBeat, AnlzData } from '$lib/rb/anlz-types';

/** Index of the FIRST beat with t >= tSec (== beats.length when none).
 * beats MUST be ordered by t (contract: AnlzBeatgrid.beats is ordered). */
export function firstBeatAtOrAfter(beats: AnlzBeat[], tSec: number): number {
	let lo = 0;
	let hi = beats.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (beats[mid].t < tSec) {
			lo = mid + 1;
		} else {
			hi = mid;
		}
	}
	return lo;
}

/** Canvas geometry for one visible beat-grid line. The full-height line and
 * its top cap share an x/width so they remain one continuous marker. */
export interface BeatLineGeometry {
	x: number;
	y: 0;
	width: number;
	height: number;
	alpha: number;
	capHeight: number;
	capAlpha: number;
	isDownbeat: boolean;
}

/** Closest two grid lines may sit, in CSS px, before the grid thins itself
 * (WAVEZOOM-02). At today's 24 s window beats sit about 22 px apart, so
 * nothing thins; zoomed out to a whole track they would be under 2 px, and
 * bar lines at 7 px still read as a wall, so the whole track shows every
 * 4 bars. */
export const BEAT_LINE_MIN_GAP_PX = 10;

/** Beat strides the grid thins through: every beat, every bar, then every
 * 4, 16 and 32 bars (in beats, for 4/4). */
const BEAT_STRIDES = [1, 4, 16, 64, 128] as const;

/** Beats per drawn line at this zoom: the smallest stride whose spacing is
 * at least BEAT_LINE_MIN_GAP_PX. 0 means nothing fits (draw no grid). */
export function beatLineStride(beatSpacingPx: number): number {
	if (!(beatSpacingPx > 0)) return 1;
	for (const stride of BEAT_STRIDES) {
		if (beatSpacingPx * stride >= BEAT_LINE_MIN_GAP_PX) return stride;
	}
	return 0;
}

/** Bar number of every beat (count of downbeats at or before it, from 0),
 * so thinned bar lines stay on the same bars as the window scrolls. */
const _barIndexMemo = new WeakMap<AnlzBeat[], { length: number; bars: Int32Array }>();
function _barIndexes(beats: AnlzBeat[]): Int32Array {
	const memo = _barIndexMemo.get(beats);
	if (memo !== undefined && memo.length === beats.length) return memo.bars;
	const bars = new Int32Array(beats.length);
	let bar = -1;
	for (let i = 0; i < beats.length; i++) {
		if (beats[i].n === 1) bar += 1;
		bars[i] = Math.max(0, bar);
	}
	_barIndexMemo.set(beats, { length: beats.length, bars });
	return bars;
}

/** Closest two consecutive beats sit anywhere in the window, in px. The
 * stride is picked from this, not the first interval, so a faster tempo
 * section later in the same window still keeps BEAT_LINE_MIN_GAP_PX. */
function _minVisibleBeatSpacingPx(beats: AnlzBeat[], start: number, tRightSec: number, pxPerSec: number): number {
	let minS = Infinity;
	const from = Math.max(0, Math.min(start, beats.length - 2));
	for (let i = from; i + 1 < beats.length; i++) {
		// Check the right edge before the gap: an interval that starts in view but
		// ends past the window is not visible spacing, and it must not thin the grid.
		if (beats[i + 1].t > tRightSec) break;
		const gap = beats[i + 1].t - beats[i].t;
		if (gap > 0 && gap < minS) minS = gap;
	}
	return Number.isFinite(minS) ? minS * pxPerSec : 0;
}

/** Project ordered, real PQTZ beats into the visible canvas window.
 * Downbeats are wider and brighter; every returned line spans rowHeight.
 * When beats would sit closer than BEAT_LINE_MIN_GAP_PX the grid keeps
 * only every bar, then every 4, 16 or 32 bars (WAVEZOOM-02). Without a
 * trusted bar phase it keeps every Nth beat instead.
 * `originPx`, when given, is the whole-pixel offset a cached image was blitted
 * at: each line then sits at its rounded track pixel minus that offset, the
 * same pixel as a Gothic block edge snapped to it (DECKUX-42). */
export function visibleBeatLines(
	beats: AnlzBeat[],
	tLeftSec: number,
	pxPerSec: number,
	widthCss: number,
	rowHeight: number,
	barPhaseUntrusted = false,
	originPx?: number
): BeatLineGeometry[] {
	if (beats.length === 0) return [];
	const tRightSec = tLeftSec + widthCss / pxPerSec;
	const lines: BeatLineGeometry[] = [];
	const start = firstBeatAtOrAfter(beats, tLeftSec);
	const stride = beatLineStride(_minVisibleBeatSpacingPx(beats, start, tRightSec, pxPerSec));
	if (stride === 0) return [];
	const bars = stride >= 4 ? _barIndexes(beats) : null;
	for (let i = start; i < beats.length; i++) {
		const beat = beats[i];
		if (beat.t > tRightSec) break;
		const trustedBar = !barPhaseUntrusted && beat.bar_phase_untrusted !== true;
		if (stride >= 4) {
			if (trustedBar) {
				if (beat.n !== 1 || bars === null || bars[i] % (stride / 4) !== 0) continue;
			} else if (i % stride !== 0) {
				continue;
			}
		}
		const isDownbeat = beat.n === 1 && trustedBar;
		// round(a - n) is round(a) - n for a whole n: the edge's own pixel.
		const x = Math.round(originPx === undefined ? (beat.t - tLeftSec) * pxPerSec : beat.t * pxPerSec - originPx);
		// A trusted bar and an untrusted beat can each pass their own stride yet sit
		// one beat apart, so every candidate is checked against the last line drawn.
		const prev = lines[lines.length - 1];
		if (prev !== undefined && x - prev.x < BEAT_LINE_MIN_GAP_PX) continue;
		lines.push({
			x,
			y: 0,
			width: isDownbeat ? 2 : 1,
			height: rowHeight,
			// Full-height body must stay readable through dense waveform bands so
			// synced decks can be compared at channel borders (ch1 vs ch2).
			alpha: isDownbeat ? 0.55 : 0.38,
			capHeight: isDownbeat ? 10 : 5,
			capAlpha: isDownbeat ? 1 : 0.85,
			isDownbeat
		});
	}
	return lines;
}

/** Enclosing PQTZ beat + fractional phase in [0,1) at positionSec. */
export function enclosingBeatPhase(
	beats: AnlzBeat[],
	positionSec: number
): { n: number; phase: number } | null {
	if (beats.length < 2 || !Number.isFinite(positionSec) || positionSec < 0) return null;
	let i = firstBeatAtOrAfter(beats, positionSec) - 1;
	if (i < 0) i = 0;
	if (i >= beats.length - 1) return null;
	const a = beats[i];
	const b = beats[i + 1];
	const dur = b.t - a.t;
	if (!(dur > 0)) return null;
	const phase = (positionSec - a.t) / dur;
	if (!Number.isFinite(phase)) return null;
	return { n: a.n, phase: Math.min(1, Math.max(0, phase)) };
}

/** Rekordbox's normal bar has four numbered PQTZ beats. Keep this beside
 * pqtzBarPhase so every visual consumer shares one explicit fallback
 * contract instead of inlining "4" wherever a bar length is needed. */
export const DEFAULT_PQTZ_BAR_BEATS = 4;

/** Fraction through the current PQTZ bar ("phase"), or null when the real
 * grid cannot establish it. Callers may park their visual at the downbeat
 * for null. `barBeats` is the config anchor (pin 67a4ce88805f) - pass the
 * deck's own beats-per-phase setting; it defaults here only for a caller
 * with none to give.
 *
 * Deliberately does NOT use `enclosingBeatPhase`'s `n` field for the cycle
 * position: captured PQTZ beats number 1..4 and RESET every bar regardless
 * of `barBeats` (rekordbox's own bar length, always 4). Keying phase on `n`
 * directly made an 8-beat phase snap backwards every 4 beats instead of
 * completing one real revolution, and made a 1-beat phase null on 3 beats
 * out of 4 (n>1 never satisfies n<=1). The SEQUENTIAL beat index in the
 * ordered array has no such reset, so `index % barBeats` is the one value
 * that actually walks 0..barBeats-1 once per configured phase, matching the
 * pin's "rotates one full journey per phase" regardless of what barBeats is. */
export function pqtzBarPhase(
	beats: AnlzBeat[],
	positionSec: number,
	barBeats: number = DEFAULT_PQTZ_BAR_BEATS
): number | null {
	if (!Number.isInteger(barBeats) || barBeats < 1) {
		throw new RangeError(`pqtzBarPhase: barBeats must be a positive integer, got ${barBeats}`);
	}
	if (beats.length < 2 || !Number.isFinite(positionSec) || positionSec < 0) return null;
	let i = firstBeatAtOrAfter(beats, positionSec) - 1;
	if (i < 0) i = 0;
	if (i >= beats.length - 1) return null;
	const a = beats[i];
	const b = beats[i + 1];
	const dur = b.t - a.t;
	if (!(dur > 0)) return null;
	const rawPhase = (positionSec - a.t) / dur;
	if (!Number.isFinite(rawPhase)) return null;
	const phase = Math.min(1, Math.max(0, rawPhase));
	return ((i % barBeats) + phase) / barBeats;
}

/** The jog wheel's central face (the off-white disc carrying the BPM/pitch
 * text) is an r=40 circle centered at 50,50 in the dial's 100x100 viewBox.
 * Pin f19a1b2a455a: "no spinning UI to overlap the central wheel", so every
 * rotating mark has to live wholly OUTSIDE this radius. Exported so the
 * no-overlap requirement is an assertable number rather than a comment. */
export const JOG_WHEEL_FACE_RADIUS = 40;

/** The face is itself stroked, so the disc RENDERS half a stroke wider than
 * r=40. Clearance is measured from that edge, not the geometric radius, or
 * a mark could sit exactly on the face's own border and still satisfy an
 * arithmetic that only knew about r=40. */
export const JOG_WHEEL_FACE_STROKE_WIDTH = 1;
export const JOG_WHEEL_FACE_RENDERED_RADIUS =
	JOG_WHEEL_FACE_RADIUS + JOG_WHEEL_FACE_STROKE_WIDTH / 2;

/** Stroke widths of the three rotating rim elements, mirrored by JogDial's
 * own CSS/attributes (jog-phase-visual asserts the two agree). They belong
 * here because the no-overlap radius cannot be chosen without them. */
export const PHASE_MARK_STROKE_WIDTH = 1.25;
export const PHASE_DOWNBEAT_STROKE_WIDTH = 3;
export const POSITION_TICK_STROKE_WIDTH = 3;

/** Every rotating rim element is drawn with `stroke-linecap: round`, which
 * caps each endpoint with a semicircle of radius strokeWidth/2. A radial
 * mark therefore RENDERS strokeWidth/2 further in than its inner endpoint:
 * the r=41 endpoint the first cut of pin f19a1b2a455a chose reached r=39.5
 * for the stroke-width-3 downbeat, i.e. 0.5 units INSIDE the r=40 face,
 * while a centerline-only assertion reported no overlap. */
export const THICKEST_ROTATING_STROKE_WIDTH = Math.max(
	PHASE_MARK_STROKE_WIDTH,
	PHASE_DOWNBEAT_STROKE_WIDTH,
	POSITION_TICK_STROKE_WIDTH
);

/** Gap left between the face edge and the nearest RENDERED pixel of any
 * rotating element, so "outside the face" is visibly true rather than
 * exactly tangent. */
export const JOG_FACE_CLEARANCE = 0.5;

/** Radii of a phase mark, measured from the dial center. Both sit in the
 * annulus between the wheel face (r=40) and the outer ring (r=47), and the
 * inner one is chosen for the THICKEST rotating stroke so every mark's cap,
 * not just its centerline, clears the face. The red position tick shares
 * this annulus. */
export const PHASE_MARK_OUTER_RADIUS = 46;
export const PHASE_MARK_INNER_RADIUS =
	JOG_WHEEL_FACE_RENDERED_RADIUS + JOG_FACE_CLEARANCE + THICKEST_ROTATING_STROKE_WIDTH / 2;

/** One phase mark on the JogDial rim: its angle in degrees before the
 * group's own phase rotation, and whether it is beat 1 (the downbeat).
 *
 * Pin f19a1b2a455a replaces pin 67a4ce88805f's centre-crossing radial grid
 * ("remove the spinning black line - looks bad, the white line is plenty"):
 * the phase now carries one WHITE rim mark per beat in the phase, beat 1
 * thicker than the rest, and nothing reaching into the wheel face. Pure
 * geometry so the mark COUNT and the downbeat flag are unit-testable
 * without rendering Svelte. */
export interface PhaseBeatMark {
	angleDeg: number;
	isDownbeat: boolean;
}

export function phaseBeatMarks(barBeats: number): PhaseBeatMark[] {
	if (!Number.isInteger(barBeats) || barBeats < 1) {
		throw new RangeError(`phaseBeatMarks: barBeats must be a positive integer, got ${barBeats}`);
	}
	return Array.from({ length: barBeats }, (_, i) => ({
		angleDeg: (360 * i) / barBeats,
		isDownbeat: i === 0
	}));
}

/** Beats in one phase for the jog visual, from the deck's own quantize grid.
 *
 * Pin f19a1b2a455a: "it should spin once per phase (4 beats default)". A
 * phase is a BAR, so a 1-beat quantize grid is not a phase length - it is
 * the snap grid at its shipped default (player/state.svelte.ts seeds every
 * deck at 1), which is exactly what made the visual spin once per BEAT with
 * a single mark. Both 1 and the unimplemented 'phase' sentinel therefore
 * mean "no phase length is set" and resolve to DEFAULT_PQTZ_BAR_BEATS,
 * while a deliberately chosen bar-scale grid (4 or 8) still anchors the
 * phase per pin 67a4ce88805f's "phase in config is anchor". */
export function jogPhaseBeats(quantizeGrid: number | 'phase'): number {
	if (quantizeGrid === 'phase' || quantizeGrid === 1) return DEFAULT_PQTZ_BAR_BEATS;
	if (!Number.isInteger(quantizeGrid) || quantizeGrid < 1) {
		throw new RangeError(`jogPhaseBeats: quantize grid must be a positive integer or 'phase', got ${quantizeGrid}`);
	}
	return quantizeGrid;
}

/** Center-playhead sync tone for a Beat Sync follower vs the master. */
export type SyncPlayheadTone = 'bar1' | 'synced' | 'drift';

/** Phase within a beat that still counts as locked (light-touch warning above). */
const SYNC_PHASE_OK = 0.12;

/**
 * Visual sync state for a follower wavestack row.
 * null = not a synced follower (master, sync off, or missing grids).
 * bar1 = locked on beat 1 of both; synced = locked but not on 1; drift = out.
 */
export function followerSyncPlayheadTone(args: {
	beatSyncEnabled: boolean;
	isMaster: boolean;
	syncError: string | null;
	syncMode: 'beat' | 'bar';
	followerBeats: AnlzBeat[];
	masterBeats: AnlzBeat[];
	followerPosMs: number;
	masterPosMs: number;
}): SyncPlayheadTone | null {
	if (!args.beatSyncEnabled || args.isMaster) return null;
	if (args.syncError !== null) return 'drift';
	const f = enclosingBeatPhase(args.followerBeats, args.followerPosMs / 1000);
	const m = enclosingBeatPhase(args.masterBeats, args.masterPosMs / 1000);
	if (f === null || m === null) return null;
	const raw = Math.abs(f.phase - m.phase);
	const phaseDelta = Math.min(raw, 1 - raw);
	const phaseOk = phaseDelta <= SYNC_PHASE_OK;
	const numberOk = args.syncMode === 'beat' || f.n === m.n;
	if (!phaseOk || !numberOk) return 'drift';
	if (f.n === 1 && m.n === 1) return 'bar1';
	return 'synced';
}

/** Clamped pixel span of an engaged loop on ONE waveform surface. */
export interface LoopBandPx {
	left: number;
	right: number;
}

/**
 * The only three fields the band geometry reads from a loop.
 *
 * Narrower than `LoopState` on purpose. Stating it structurally lets the
 * painters that draw this band stay presentational modules which never import
 * the API contract, so a surface can be unit-tested without dragging the whole
 * deck type surface behind it. `LoopState` satisfies this shape, so every
 * existing caller keeps passing one unchanged.
 */
export interface LoopBandSource {
	in_ms: number;
	out_ms: number;
	engaged: boolean;
}

/**
 * Minimum drawn width of a loop band, in surface pixels.
 *
 * The overview strip squeezes a whole track into a few hundred pixels, so a
 * short loop can round away to nothing. This widens the DRAWN band only, never
 * the loop itself, and only when the true span would otherwise be invisible.
 */
export const LOOP_MIN_BAND_PX = 2;

/**
 * Project an engaged loop into one surface's visible pixel range.
 *
 * Each waveform surface maps track time to x differently (the wavestack row
 * scrolls a 24s window; the overview strip spans the whole track), so the
 * caller supplies `toPx`. What must NOT vary between surfaces is the decision
 * itself, which is why the engaged check, the clamp and the minimum width all
 * live here: a surface that reimplements them and forgets one is exactly the
 * defect this helper exists to prevent (DECKUX-04 - the loop painted on the
 * wavestack row and was invisible on the deck's own overview strip).
 *
 * Returns null when nothing is engaged, the span is empty, or the loop lies
 * entirely outside this surface.
 */
export function loopBandPx(
	loop: LoopBandSource | null,
	toPx: (ms: number) => number,
	widthPx: number,
	minWidthPx: number = LOOP_MIN_BAND_PX
): LoopBandPx | null {
	if (loop === null || !loop.engaged) return null;
	if (!(loop.out_ms > loop.in_ms)) return null;
	const x0 = toPx(loop.in_ms);
	const x1 = toPx(loop.out_ms);
	if (!Number.isFinite(x0) || !Number.isFinite(x1)) {
		throw new RangeError(`loopBandPx: toPx must return finite px, got ${x0}..${x1}`);
	}
	if (x1 <= 0 || x0 >= widthPx) return null; // wholly off this surface
	let left = Math.max(0, Math.min(widthPx, x0));
	let right = Math.max(0, Math.min(widthPx, x1));
	if (right - left < minWidthPx) {
		right = Math.min(widthPx, left + minWidthPx);
		left = Math.max(0, right - minWidthPx);
	}
	if (right <= left) return null;
	return { left, right };
}
