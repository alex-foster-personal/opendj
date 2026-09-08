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

/** Project ordered, real PQTZ beats into the visible canvas window.
 * Downbeats are wider and brighter; every returned line spans rowHeight. */
export function visibleBeatLines(
	beats: AnlzBeat[],
	tLeftSec: number,
	pxPerSec: number,
	widthCss: number,
	rowHeight: number
): BeatLineGeometry[] {
	if (beats.length === 0) return [];
	const tRightSec = tLeftSec + widthCss / pxPerSec;
	const lines: BeatLineGeometry[] = [];
	for (let i = firstBeatAtOrAfter(beats, tLeftSec); i < beats.length; i++) {
		const beat = beats[i];
		if (beat.t > tRightSec) break;
		const isDownbeat = beat.n === 1;
		lines.push({
			x: Math.round((beat.t - tLeftSec) * pxPerSec),
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

/** Gutter label like '1Bars' / '86Bars' - WHOLE bars until the next
 * countdown target (SCREENSHOT-SPEC 2: bars until next cue/phrase).
 *
 * Whole bars, floored, and the render cost is the reason as much as the
 * readability is (pin d56d98cd9c53, the maintainer, Wed 2 Sep 2026). The label used to
 * carry the leftover beat ('3.2Bars'), so its string changed on every beat
 * and dirtied the gutter four times a bar for a digit nobody reads at a
 * glance. Flooring to bars makes it change only when the playhead crosses a
 * bar line: one re-render a bar instead of four, with no throttle to keep in
 * sync. Floor, never round - a rounded label would claim a bar of runway
 * that is not there.
 *
 * Target picking, in priority order over REAL data only:
 *   1. earliest upcoming cue (memory / hot cue / loop-in), else
 *   2. earliest upcoming phrase boundary, else
 *   3. the end of the beatgrid (last analyzed beat).
 * Most library tracks carry zero djmdCue rows, so without the phrase and
 * end-of-grid fallbacks the counter would almost never render - rekordbox
 * shows it whenever a beatgrid exists. Returns null only when the track
 * has no beatgrid or the playhead is past every target - never invented. */
export function barsToNextCueLabel(anlz: AnlzData, positionMs: number): string | null {
	const beats = anlz.beatgrid.beats;
	if (beats.length === 0) return null;
	const posS = positionMs / 1000;
	let targetS = Infinity;
	for (const cue of anlz.cues) {
		const cueS = cue.in_ms / 1000;
		if (cueS > posS && cueS < targetS) targetS = cueS;
	}
	if (!Number.isFinite(targetS)) {
		for (const phrase of anlz.phrases) {
			if (phrase.start_s > posS && phrase.start_s < targetS) targetS = phrase.start_s;
		}
	}
	if (!Number.isFinite(targetS)) {
		const gridEndS = beats[beats.length - 1].t;
		if (gridEndS > posS) targetS = gridEndS;
	}
	if (!Number.isFinite(targetS)) return null;
	const beatsRemaining = firstBeatAtOrAfter(beats, targetS) - firstBeatAtOrAfter(beats, posS);
	return `${Math.floor(beatsRemaining / 4)}Bars`;
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

/** Radii of a phase mark, measured from the dial center. Both sit in the
 * annulus between the wheel face (r=40) and the outer ring (r=47). */
export const PHASE_MARK_OUTER_RADIUS = 46;
export const PHASE_MARK_INNER_RADIUS = 41;

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
